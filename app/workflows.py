from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.discord_api import DiscordClient
from app.tasks import TaskDispatcher
from app.verifier import ProofVerifier, VerificationResult, normalize_mime_type

logger = logging.getLogger(__name__)

COLOR_INFO = 0x03B2F8
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xF1C40F
COLOR_DANGER = 0xE74C3C

VERIFIED_DM = (
    "Hi there! You've been given the **Verified** role on the Googlers Discord Server! "
    "Please take a moment to join our verified channels by clicking **Channels & Roles** at the top of the server."
)

PASSED_HC_DM_BASE = "Hi there! You've been given the **Passed Hiring Committee** role on the Googlers Discord Server"
PASSED_HC_DM_TAIL = ". If you were expecting full verification, please re-read the verification rules and resubmit with proof of offer acceptance or employment."


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sanitize_filename(name: str, index: int) -> str:
    safe = "".join(ch if ch.isalnum() or ch in (".", "-", "_") else "_" for ch in (name or ""))
    safe = safe.strip("._") or f"proof_{index}.png"
    return safe[:80]


def build_mod_buttons(target_user_id: str) -> list[dict[str, Any]]:
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,  # Success (green)
                    "label": "Verify",
                    "emoji": {"name": "✅"},
                    "custom_id": f"mod_verify:{target_user_id}",
                },
                {
                    "type": 2,
                    "style": 1,  # Primary (blurple)
                    "label": "Passed HC",
                    "emoji": {"name": "🟡"},
                    "custom_id": f"mod_hc:{target_user_id}",
                },
                {
                    "type": 2,
                    "style": 4,  # Danger (red)
                    "label": "Reject",
                    "emoji": {"name": "❌"},
                    "custom_id": f"mod_reject:{target_user_id}",
                },
            ],
        }
    ]


def build_auto_reject_override_buttons(target_user_id: str) -> list[dict[str, Any]]:
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,  # Success (green)
                    "label": "Override: Verify",
                    "emoji": {"name": "✅"},
                    "custom_id": f"mod_verify:{target_user_id}",
                },
                {
                    "type": 2,
                    "style": 1,  # Primary (blurple)
                    "label": "Override: Passed HC",
                    "emoji": {"name": "🟡"},
                    "custom_id": f"mod_hc:{target_user_id}",
                },
                {
                    "type": 2,
                    "style": 4,  # Danger (red)
                    "label": "Reject w/ Note",
                    "emoji": {"name": "❌"},
                    "custom_id": f"mod_reject:{target_user_id}",
                },
                {
                    "type": 2,
                    "style": 2,  # Secondary (gray)
                    "label": "Dismiss",
                    "emoji": {"name": "🗑️"},
                    "custom_id": f"mod_dismiss:{target_user_id}",
                },
            ],
        }
    ]


def build_panel_message(settings: Settings) -> dict[str, Any]:
    buttons: list[dict[str, Any]] = [
        {
            "type": 2,
            "style": 1,  # Primary
            "label": "Verify with Proof (Offers / Alumni / Passed HC)",
            "emoji": {"name": "📸"},
            "custom_id": "open_proof_modal",
        }
    ]
    if settings.internal_form_url_template:
        buttons.append(
            {
                "type": 2,
                "style": 3,  # Success
                "label": "Current Googler (@google.com Form)",
                "emoji": {"name": "🏢"},
                "custom_id": "open_internal_link",
            }
        )

    description = (
        "Click a button below to verify your status on the **Googlers Discord Server**.\n\n"
        "**For Full Verification (`Verified` role)** — attach proof of employment or offer acceptance:\n"
        "• Screenshot of your Google onboarding portal (`google.com/about/careers/applications/onboarding/`)\n"
        "• DocuSign email/page with **Completed** / signed Google offer letter\n"
        "• Recruiter email (`@google.com` or `@xwf.google.com`) confirming you accepted/are joining\n"
        "• Employment verification letter, or other proof only a current/former Googler could provide\n\n"
        "**For `Passed HC` role** — attach a Google recruiter email moving you to team matching / project search, or an unsigned offer letter.\n\n"
        "*Feel free to blur or redact your personal name, address, and compensation. Submissions stay private between you and the moderators.*"
    )
    if settings.internal_form_url_template:
        description += (
            "\n\n**Already have a `@google.com` corp account?** Click **Current Googler (@google.com Form)** "
            "for instant verification without uploading a screenshot."
        )

    return {
        "embeds": [
            {
                "title": "Googlers Server Verification",
                "description": description,
                "color": COLOR_INFO,
            }
        ],
        "components": [{"type": 1, "components": buttons}],
    }


async def _find_and_archive_pending_card(
    discord: DiscordClient,
    settings: Settings,
    *,
    target_user_id: str,
    source_message_id: str | None,
) -> tuple[list[dict[str, Any]], list[tuple[str, bytes, str]], str | None]:
    """Locate the pending verification message (by exact ID or recent scan),
    extract its embeds + re-download its attachments, and return (embeds, files, message_id).
    """
    msg: dict[str, Any] | None = None
    if source_message_id:
        msg = await discord.get_channel_message(settings.channel_verifications, source_message_id)
    else:
        recent = await discord.get_channel_messages(settings.channel_verifications, limit=25)
        mention = f"<@{target_user_id}>"
        for candidate in recent:
            embeds = candidate.get("embeds") or []
            embed_text = " ".join(
                (e.get("description") or "")
                + " "
                + " ".join(f"{f.get('name', '')}:{f.get('value', '')}" for f in e.get("fields", []))
                for e in embeds
            )
            if mention in embed_text or target_user_id in embed_text:
                msg = candidate
                break

    if not msg:
        return [], [], None

    archived_embeds: list[dict[str, Any]] = []
    for emb in msg.get("embeds") or []:
        # Copy embed fields cleanly for re-posting to #logs
        clean: dict[str, Any] = {
            k: v
            for k, v in emb.items()
            if k in ("title", "description", "color", "fields", "timestamp", "footer")
        }
        archived_embeds.append(clean)

    downloaded_files: list[tuple[str, bytes, str]] = []
    for idx, att in enumerate(msg.get("attachments") or []):
        att_url = att.get("url")
        if not att_url:
            continue
        try:
            data = await discord.download_attachment(
                att_url, max_bytes=settings.max_attachment_bytes
            )
            filename = _sanitize_filename(att.get("filename", f"proof_{idx}.png"), idx)
            mime = normalize_mime_type(att.get("content_type"), filename)
            downloaded_files.append((filename, data, mime))
            if archived_embeds and idx == 0 and mime.startswith("image/"):
                archived_embeds[0]["image"] = {"url": f"attachment://{filename}"}
        except Exception as exc:
            logger.warning("Could not re-download attachment %s for archive: %s", att_url, exc)

    return archived_embeds, downloaded_files, msg.get("id")


async def process_verification_task(
    payload: dict[str, Any],
    settings: Settings,
    discord: DiscordClient,
    verifier: ProofVerifier,
    dispatcher: TaskDispatcher | None = None,
) -> dict[str, Any]:
    app_id = str(payload["application_id"])
    interaction_token = str(payload["interaction_token"])
    user_id = str(payload["user_id"])
    username = str(payload.get("username") or user_id)
    display_name = str(payload.get("display_name") or username)
    additional_info = str(payload.get("additional_info") or "").strip()
    attachments_meta: list[dict[str, Any]] = list(payload.get("attachments") or [])

    downloaded: list[tuple[str, bytes, str]] = []
    for idx, meta in enumerate(attachments_meta[: settings.max_attachments]):
        filename = _sanitize_filename(meta.get("filename", f"proof_{idx}.png"), idx)
        mime = normalize_mime_type(meta.get("content_type"), filename)
        try:
            data = await discord.download_attachment(
                str(meta["url"]), max_bytes=settings.max_attachment_bytes
            )
            downloaded.append((filename, data, mime))
        except Exception as exc:
            logger.exception("Failed downloading attachment for user %s: %s", user_id, exc)
            await discord.edit_interaction_response(
                app_id,
                interaction_token,
                content=(
                    "❌ I couldn't download your uploaded file (it may be too large or unsupported). "
                    "Please try uploading a PNG, JPEG, or PDF under 10 MB."
                ),
            )
            return {"status": "download_failed"}

    if not downloaded:
        await discord.edit_interaction_response(
            app_id,
            interaction_token,
            content="❌ No valid proof attachment was received. Please try submitting again.",
        )
        return {"status": "no_attachments"}

    # Evaluate with Vertex AI Gemini
    eval_error: str | None = None
    result: VerificationResult | None = None
    try:
        result = verifier.evaluate(downloaded, additional_info=additional_info)
    except Exception as exc:
        logger.exception("Vertex AI Gemini evaluation failed for user %s: %s", user_id, exc)
        eval_error = f"{type(exc).__name__}: {exc}"

    first_image_filename = next(
        (fn for fn, _, mime in downloaded if mime.startswith("image/")), None
    )

    # Auto-approve only on HIGH confidence VERIFY or PASSED_HC
    if (
        result is not None
        and result.confidence == "HIGH"
        and result.verdict in ("VERIFY", "PASSED_HC")
    ):
        if result.verdict == "VERIFY":
            add_role_name, add_role_id = "verified", settings.role_verified
            remove_names = ["passed HC", "unverified"]
            remove_ids = [settings.role_passed_hc, settings.role_unverified]
            user_ephemeral_msg = (
                "✅ **You're verified!** You've been granted the **Verified** role. "
                "Head to **Channels & Roles** at the top of the server to join the verified channels."
            )
            dm_msg = VERIFIED_DM
            color = COLOR_SUCCESS
        else:
            add_role_name, add_role_id = "passed HC", settings.role_passed_hc
            remove_names = ["verified", "unverified"]
            remove_ids = [settings.role_verified, settings.role_unverified]
            user_ephemeral_msg = (
                "🟡 **Passed HC role granted!** If you were expecting full verification, "
                "please review the rules and resubmit with proof of offer acceptance or employment."
            )
            dm_msg = PASSED_HC_DM_BASE + PASSED_HC_DM_TAIL
            color = COLOR_WARNING

        await discord.update_member_roles(
            settings.discord_guild_id, user_id, add_role_id, remove_ids
        )
        dm_ok, dm_err = await discord.send_dm(user_id, dm_msg)

        log_embed: dict[str, Any] = {
            "title": f"Auto-Verification: {result.verdict}",
            "description": (
                f"**User:** <@{user_id}> (`{username}` / `{display_name}`)\n"
                f"**Additional Info:** {additional_info or '_None_'}\n"
                f"**Gemini Notes:** {result.reason}"
            ),
            "color": color,
            "fields": [
                {"name": "Verdict", "value": result.verdict, "inline": True},
                {"name": "Confidence", "value": result.confidence, "inline": True},
                {"name": "Proof Type", "value": result.proof_type, "inline": True},
                {"name": "User ID", "value": user_id, "inline": True},
            ],
            "timestamp": _now_iso(),
        }
        if first_image_filename:
            log_embed["image"] = {"url": f"attachment://{first_image_filename}"}

        dm_note = "" if dm_ok else f" *(DM skipped: {dm_err})*"
        await discord.send_channel_message(
            settings.channel_logs,
            content=(
                f"✅ | User <@{user_id}> updated roles: `+{add_role_name}`, "
                f"`-({', '.join(remove_names)})`. Request processed by **automatic verification**.{dm_note}"
            ),
            embeds=[log_embed],
            files=downloaded,
        )
        await discord.edit_interaction_response(
            app_id, interaction_token, content=user_ephemeral_msg
        )
        return {"status": "auto_processed", "verdict": result.verdict}

    # If Vertex AI itself errored, fall back to manual review (never auto-reject on an internal error)
    if result is None:
        reason_text = f"Automated review failed ({eval_error}); please review manually."
        review_embed: dict[str, Any] = {
            "title": "Verification Request (Manual Review — AI Error)",
            "description": (
                f"**User:** <@{user_id}> (`{username}` / `{display_name}`)\n"
                f"**Additional Info:** {additional_info or '_None_'}\n"
                f"**Assessment:** {reason_text}"
            ),
            "color": COLOR_INFO,
            "fields": [
                {"name": "Suggested Verdict", "value": "AI_ERROR", "inline": True},
                {"name": "Confidence", "value": "N/A", "inline": True},
                {"name": "Proof Type", "value": "error", "inline": True},
                {"name": "User ID", "value": user_id, "inline": True},
            ],
            "timestamp": _now_iso(),
        }
        if first_image_filename:
            review_embed["image"] = {"url": f"attachment://{first_image_filename}"}

        await discord.send_channel_message(
            settings.channel_verifications,
            embeds=[review_embed],
            components=build_mod_buttons(user_id),
            files=downloaded,
        )
        await discord.edit_interaction_response(
            app_id,
            interaction_token,
            content=(
                "📩 **Submitted for moderator review!** Our automated reviewer ran into a temporary "
                "issue, so your proof has been sent privately to the moderators. You'll receive a DM once reviewed."
            ),
        )
        return {"status": "queued_for_manual_review", "verdict": "AI_ERROR"}

    # Bar for HIGH-confidence VERIFY or PASSED_HC was not met -> reject by default with Gemini justification,
    # and post an override card in #verifications with a 48h auto-delete TTL.
    member = await discord.get_guild_member(settings.discord_guild_id, user_id)
    existing_roles = {str(r) for r in ((member or {}).get("roles") or [])}
    # Never strip an existing verified or Passed Interviews role on a failed upgrade attempt
    if (
        settings.role_verified not in existing_roles
        and settings.role_passed_hc not in existing_roles
    ):
        await discord.update_member_roles(
            settings.discord_guild_id,
            user_id,
            settings.role_unverified,
            [],
        )

    ttl_hours = max(1, settings.auto_reject_ttl_hours)
    ttl_seconds = ttl_hours * 3600
    expire_unix = int(time.time()) + ttl_seconds

    rejection_msg = (
        "❌ **Verification not approved automatically.**\n\n"
        f"**Reason:** {result.reason}\n\n"
        f"A moderator can still review and override this within the next **{ttl_hours} hours**, "
        "or you can resubmit anytime with clearer proof of offer acceptance, employment, or Hiring Committee pass."
    )
    dm_ok, dm_err = await discord.send_dm(user_id, rejection_msg)
    await discord.edit_interaction_response(app_id, interaction_token, content=rejection_msg)

    # Archive immediately to #verifications-logs for a permanent audit trail
    dm_note = "" if dm_ok else f" *(DM skipped: {dm_err})*"
    log_reject_embed: dict[str, Any] = {
        "title": f"Auto-Rejected Verification ({result.verdict} / {result.confidence})",
        "description": (
            f"**User:** <@{user_id}> (`{username}` / `{display_name}`)\n"
            f"**Additional Info:** {additional_info or '_None_'}\n"
            f"**Gemini Justification:** {result.reason}"
        ),
        "color": COLOR_DANGER,
        "fields": [
            {"name": "Verdict", "value": result.verdict, "inline": True},
            {"name": "Confidence", "value": result.confidence, "inline": True},
            {"name": "Proof Type", "value": result.proof_type, "inline": True},
            {"name": "User ID", "value": user_id, "inline": True},
        ],
        "timestamp": _now_iso(),
    }
    if first_image_filename:
        log_reject_embed["image"] = {"url": f"attachment://{first_image_filename}"}

    await discord.send_channel_message(
        settings.channel_logs,
        content=(
            f"❌ | User <@{user_id}> auto-rejected (bar for `verified` / `passed HC` not met). "
            f"Override window open in <#{settings.channel_verifications}> for {ttl_hours}h.{dm_note}"
        ),
        embeds=[log_reject_embed],
        files=downloaded,
    )

    # Post 48h mod-override card to #verifications
    override_embed: dict[str, Any] = {
        "title": f"Auto-Rejected — {ttl_hours}h Mod Override Window",
        "description": (
            f"**User:** <@{user_id}> (`{username}` / `{display_name}`)\n"
            f"**Additional Info:** {additional_info or '_None_'}\n"
            f"**Gemini Justification (sent to user):** {result.reason}\n"
            f"**Auto-deletes:** <t:{expire_unix}:R>"
        ),
        "color": COLOR_DANGER,
        "fields": [
            {"name": "Verdict", "value": result.verdict, "inline": True},
            {"name": "Confidence", "value": result.confidence, "inline": True},
            {"name": "Proof Type", "value": result.proof_type, "inline": True},
            {"name": "User ID", "value": user_id, "inline": True},
        ],
        "timestamp": _now_iso(),
    }
    if first_image_filename:
        override_embed["image"] = {"url": f"attachment://{first_image_filename}"}

    posted = await discord.send_channel_message(
        settings.channel_verifications,
        embeds=[override_embed],
        components=build_auto_reject_override_buttons(user_id),
        files=downloaded,
    )

    posted_id = str(posted.get("id") or "")
    if posted_id and dispatcher is not None:
        try:
            dispatcher.enqueue(
                "/tasks/expire-verification",
                {
                    "channel_id": settings.channel_verifications,
                    "message_id": posted_id,
                    "user_id": user_id,
                },
                delay_seconds=ttl_seconds,
            )
        except Exception as exc:
            logger.exception(
                "Failed scheduling %dh TTL expiry for message %s: %s", ttl_hours, posted_id, exc
            )

    return {
        "status": "auto_rejected",
        "verdict": result.verdict,
        "confidence": result.confidence,
        "override_message_id": posted_id,
    }


async def process_expire_verification_task(
    payload: dict[str, Any],
    settings: Settings,
    discord: DiscordClient,
) -> dict[str, Any]:
    channel_id = str(payload.get("channel_id") or settings.channel_verifications)
    message_id = str(payload.get("message_id") or "")
    if not message_id:
        return {"status": "missing_message_id"}

    deleted = await discord.delete_channel_message(channel_id, message_id)
    if deleted:
        logger.info(
            "Auto-expired override message %s from channel %s after TTL", message_id, channel_id
        )
        return {"status": "expired_deleted", "message_id": message_id}
    return {"status": "already_handled", "message_id": message_id}


async def process_mod_action_task(
    payload: dict[str, Any],
    settings: Settings,
    discord: DiscordClient,
) -> dict[str, Any]:
    action = str(payload["action"])  # "verify" | "passed_hc" | "reject" | "dismiss"
    target_user_id = str(payload["target_user_id"])
    moderator_id = str(payload["moderator_id"])
    moderator_name = str(payload.get("moderator_name") or moderator_id)
    reason = str(payload.get("reason") or "").strip()
    source_message_id = payload.get("source_message_id")
    app_id = str(payload["application_id"])
    interaction_token = str(payload["interaction_token"])

    if action == "dismiss":
        deleted = (
            await discord.delete_channel_message(
                settings.channel_verifications, str(source_message_id)
            )
            if source_message_id
            else False
        )
        msg = (
            f"🗑️ Dismissed auto-rejected verification card for <@{target_user_id}>."
            if deleted
            else "ℹ️ This verification card was already removed."
        )
        await discord.edit_interaction_response(app_id, interaction_token, content=msg)
        return {"status": "dismissed" if deleted else "already_processed"}

    if action == "verify":
        add_role_name, add_role_id = "verified", settings.role_verified
        remove_names = ["passed HC", "unverified"]
        remove_ids = [settings.role_passed_hc, settings.role_unverified]
        dm_text = VERIFIED_DM
        icon = "✅"
    elif action == "passed_hc":
        add_role_name, add_role_id = "passed HC", settings.role_passed_hc
        remove_names = ["verified", "unverified"]
        remove_ids = [settings.role_verified, settings.role_unverified]
        reason_part = f" with the following reason: {reason}" if reason else ""
        dm_text = f"{PASSED_HC_DM_BASE}{reason_part}{PASSED_HC_DM_TAIL}"
        icon = "🟡"
    elif action == "reject":
        add_role_name, add_role_id = "unverified", settings.role_unverified
        remove_names = ["verified", "passed HC"]
        remove_ids = [settings.role_verified, settings.role_passed_hc]
        dm_text = (
            f"Hi there! Your verification request on the Googlers Discord Server was rejected "
            f"with the following reason: **{reason or 'Insufficient proof provided'}**. "
            "Please resubmit if you'd like your request reconsidered."
        )
        icon = "❌"
    else:
        raise ValueError(f"Unknown mod action: {action}")

    # Verify target is still in the guild before modifying roles
    member = await discord.get_guild_member(settings.discord_guild_id, target_user_id)
    if member is None:
        if source_message_id:
            await discord.delete_channel_message(
                settings.channel_verifications, str(source_message_id)
            )
        await discord.edit_interaction_response(
            app_id,
            interaction_token,
            content=f"⚠️ User <@{target_user_id}> is no longer in the server. Pending card cleaned up.",
        )
        return {"status": "member_not_found"}

    archived_embeds, archived_files, found_msg_id = await _find_and_archive_pending_card(
        discord,
        settings,
        target_user_id=target_user_id,
        source_message_id=str(source_message_id) if source_message_id else None,
    )

    if source_message_id and found_msg_id is None:
        await discord.edit_interaction_response(
            app_id,
            interaction_token,
            content="ℹ️ This verification request was already handled by another moderator.",
        )
        return {"status": "already_processed"}

    await discord.update_member_roles(
        settings.discord_guild_id, target_user_id, add_role_id, remove_ids
    )

    dm_ok, dm_err = await discord.send_dm(target_user_id, dm_text)

    reason_log = f" Reason: `{reason}`." if reason else ""
    dm_note = "" if dm_ok else f" *(DM skipped: {dm_err})*"
    await discord.send_channel_message(
        settings.channel_logs,
        content=(
            f"{icon} | User <@{target_user_id}> updated roles: `+{add_role_name}`, "
            f"`-({', '.join(remove_names)})`. Action by <@{moderator_id}> (`{moderator_name}`).{reason_log}{dm_note}"
        ),
        embeds=archived_embeds or None,
        files=archived_files or None,
    )

    if found_msg_id:
        await discord.delete_channel_message(settings.channel_verifications, found_msg_id)

    await discord.edit_interaction_response(
        app_id,
        interaction_token,
        content=(
            f"{icon} Updated <@{target_user_id}> → `+{add_role_name}`"
            + (f" (reason: {reason})" if reason else "")
            + (f". Note: could not DM user ({dm_err})." if not dm_ok else ".")
        ),
    )
    return {"status": "ok", "action": action}


async def process_internal_verification(
    *,
    user_id: str,
    notes: str,
    settings: Settings,
    discord: DiscordClient,
) -> dict[str, Any]:
    member = await discord.get_guild_member(settings.discord_guild_id, user_id)
    if member is None:
        return {
            "status": "not_found",
            "message": "User is not a member of the Googlers Discord server.",
        }

    await discord.update_member_roles(
        settings.discord_guild_id,
        user_id,
        settings.role_verified,
        [settings.role_passed_hc, settings.role_unverified],
    )
    dm_ok, dm_err = await discord.send_dm(user_id, VERIFIED_DM)

    dm_note = "" if dm_ok else f" *(DM skipped: {dm_err})*"
    notes_line = f"\n**Notes:** {notes[:300]}" if notes.strip() else ""
    await discord.send_channel_message(
        settings.channel_logs,
        content=(
            f"✅ | User <@{user_id}> updated roles: `+verified`, `-(passed HC, unverified)`. "
            f"Verified via **internal @google.com form**.{dm_note}"
        ),
        embeds=[
            {
                "title": "Internal @google.com Form Verification",
                "description": f"**User:** <@{user_id}> (`{user_id}`){notes_line}",
                "color": COLOR_SUCCESS,
                "timestamp": _now_iso(),
            }
        ],
    )
    return {"status": "verified", "user_id": user_id, "dm_sent": dm_ok}
