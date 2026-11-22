from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.discord_api import DiscordClient
from app.tasks import TaskDispatcher
from app.verifier import ProofVerifier
from app.workflows import (
    build_panel_message,
    process_mod_action_task,
    process_verification_task,
)

logger = logging.getLogger(__name__)

# Discord interaction types
INTERACTION_PING = 1
INTERACTION_APPLICATION_COMMAND = 2
INTERACTION_MESSAGE_COMPONENT = 3
INTERACTION_MODAL_SUBMIT = 5

# Discord interaction callback types
CALLBACK_PONG = 1
CALLBACK_CHANNEL_MESSAGE = 4
CALLBACK_DEFERRED_CHANNEL_MESSAGE = 5
CALLBACK_MODAL = 9

FLAG_EPHEMERAL = 1 << 6

PERM_ADMINISTRATOR = 0x8
PERM_MANAGE_ROLES = 0x10000000


def has_mod_permission(interaction: dict[str, Any]) -> bool:
    member = interaction.get("member") or {}
    perms_raw = member.get("permissions", "0")
    try:
        perms = int(perms_raw)
    except (TypeError, ValueError):
        return False
    return bool(perms & (PERM_MANAGE_ROLES | PERM_ADMINISTRATOR))


def ephemeral_message(
    content: str, components: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    data: dict[str, Any] = {"content": content, "flags": FLAG_EPHEMERAL}
    if components:
        data["components"] = components
    return {"type": CALLBACK_CHANNEL_MESSAGE, "data": data}


def deferred_ephemeral() -> dict[str, Any]:
    return {"type": CALLBACK_DEFERRED_CHANNEL_MESSAGE, "data": {"flags": FLAG_EPHEMERAL}}


def extract_modal_inputs(data: dict[str, Any]) -> dict[str, Any]:
    """Extract custom_id -> value/values from both modern Label (type 18) and legacy ActionRow (type 1) modals."""
    results: dict[str, Any] = {}
    for row in data.get("components") or []:
        comp_type = row.get("type")
        if comp_type == 18 and isinstance(row.get("component"), dict):
            inner = row["component"]
            cid = inner.get("custom_id")
            if cid:
                results[cid] = inner.get("values") if "values" in inner else inner.get("value", "")
        elif comp_type == 1 and isinstance(row.get("components"), list):
            for inner in row["components"]:
                cid = inner.get("custom_id")
                if cid:
                    results[cid] = (
                        inner.get("values") if "values" in inner else inner.get("value", "")
                    )
    return results


def _build_proof_upload_modal() -> dict[str, Any]:
    return {
        "type": CALLBACK_MODAL,
        "data": {
            "custom_id": "submit_proof_modal",
            "title": "Googlers Server Verification",
            "components": [
                {
                    "type": 18,
                    "label": "Proof of Employment or Offer",
                    "description": "Upload 1-2 screenshots or PDFs (blur name/address if you like).",
                    "component": {
                        "type": 19,
                        "custom_id": "proof_file",
                        "min_values": 1,
                        "max_values": 2,
                        "required": True,
                        "file_types": ["image", ".pdf"],
                    },
                },
                {
                    "type": 18,
                    "label": "Additional Info (optional)",
                    "description": "Any helpful context for reviewers (e.g., Xoogler, intern, team match).",
                    "component": {
                        "type": 4,
                        "custom_id": "additional_info",
                        "style": 2,
                        "placeholder": "Optional context...",
                        "required": False,
                        "max_length": 500,
                    },
                },
            ],
        },
    }


def _build_mod_reason_modal(
    *,
    custom_id: str,
    title: str,
    label: str,
    description: str,
    required: bool,
) -> dict[str, Any]:
    return {
        "type": CALLBACK_MODAL,
        "data": {
            "custom_id": custom_id,
            "title": title,
            "components": [
                {
                    "type": 18,
                    "label": label,
                    "description": description,
                    "component": {
                        "type": 4,
                        "custom_id": "reason",
                        "style": 2,
                        "placeholder": "Enter reason...",
                        "required": required,
                        "max_length": 400,
                    },
                }
            ],
        },
    }


def _get_caller(interaction: dict[str, Any]) -> tuple[str, str, str, list[str]]:
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    user_id = str(user.get("id") or "")
    username = str(user.get("username") or user_id)
    display_name = str(member.get("nick") or user.get("global_name") or username)
    roles = [str(r) for r in (member.get("roles") or [])]
    return user_id, username, display_name, roles


async def _dispatch_task(
    path: str,
    payload: dict[str, Any],
    settings: Settings,
    dispatcher: TaskDispatcher,
    discord: DiscordClient,
    verifier: ProofVerifier,
) -> None:
    if settings.inline_tasks:
        if path == "/tasks/process-verification":
            await process_verification_task(payload, settings, discord, verifier)
        elif path == "/tasks/process-mod-action":
            await process_mod_action_task(payload, settings, discord)
        else:
            raise ValueError(f"Unknown inline task path: {path}")
        return
    dispatcher.enqueue(path, payload)


async def handle_interaction(
    interaction: dict[str, Any],
    *,
    settings: Settings,
    dispatcher: TaskDispatcher,
    discord: DiscordClient,
    verifier: ProofVerifier,
) -> dict[str, Any]:
    itype = interaction.get("type")

    if itype == INTERACTION_PING:
        return {"type": CALLBACK_PONG}

    # Ensure interactions only run inside the configured guild
    guild_id = str(interaction.get("guild_id") or "")
    if guild_id and guild_id != settings.discord_guild_id:
        return ephemeral_message("This bot is only configured for the Googlers Discord server.")

    caller_id, caller_username, caller_display, caller_roles = _get_caller(interaction)
    data = interaction.get("data") or {}

    # 1. Slash commands
    if itype == INTERACTION_APPLICATION_COMMAND:
        cmd = data.get("name")
        if not has_mod_permission(interaction):
            return ephemeral_message(
                "You need the **Manage Roles** permission to use this command."
            )

        if cmd == "setup_panel":
            channel_id = str(interaction.get("channel_id") or "")
            if not channel_id:
                return ephemeral_message("Could not determine current channel.")
            panel = build_panel_message(settings)
            await discord.send_channel_message(
                channel_id,
                embeds=panel["embeds"],
                components=panel["components"],
            )
            return ephemeral_message("✅ Verification panel posted in this channel!")

        if cmd in ("verify", "passed_hc", "reject"):
            opts = {o["name"]: o.get("value") for o in (data.get("options") or [])}
            target_user_id = str(opts.get("user") or opts.get("username") or "")
            reason = str(opts.get("reason") or "")
            if not target_user_id:
                return ephemeral_message("Missing target user.")

            task_payload = {
                "action": cmd,
                "target_user_id": target_user_id,
                "moderator_id": caller_id,
                "moderator_name": caller_display,
                "reason": reason,
                "source_message_id": None,
                "application_id": str(interaction.get("application_id") or settings.discord_app_id),
                "interaction_token": str(interaction["token"]),
            }
            await _dispatch_task(
                "/tasks/process-mod-action", task_payload, settings, dispatcher, discord, verifier
            )
            return deferred_ephemeral()

        return ephemeral_message(f"Unknown command: `{cmd}`")

    # 2. Button clicks
    if itype == INTERACTION_MESSAGE_COMPONENT:
        custom_id = str(data.get("custom_id") or "")

        if custom_id == "open_proof_modal":
            if settings.role_verified in caller_roles:
                return ephemeral_message("You already have the **Verified** role! 🎉")
            return _build_proof_upload_modal()

        if custom_id == "open_internal_link":
            if settings.role_verified in caller_roles:
                return ephemeral_message("You already have the **Verified** role! 🎉")
            if not settings.internal_form_url_template:
                return ephemeral_message(
                    "The internal `@google.com` form link isn't configured yet. "
                    "Please click **Verify with Proof** to upload a screenshot instead!"
                )
            prefilled_url = settings.internal_form_url_template.format(user_id=caller_id)
            return ephemeral_message(
                content=(
                    f"Click the button below to open the internal **`@google.com`** Google Form.\n"
                    f"Your Discord User ID (`{caller_id}`) is already pre-filled — just sign in "
                    "with your `@google.com` account and hit **Submit** for instant verification!"
                ),
                components=[
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "style": 5,  # Link button
                                "label": "Open Internal @google.com Form",
                                "url": prefilled_url,
                            }
                        ],
                    }
                ],
            )

        # Moderator action buttons in #verifications
        if custom_id.startswith(("mod_verify:", "mod_hc:", "mod_reject:")):
            if not has_mod_permission(interaction):
                return ephemeral_message(
                    "You need the **Manage Roles** permission to perform this action."
                )

            prefix, _, target_user_id = custom_id.partition(":")
            source_message_id = str((interaction.get("message") or {}).get("id") or "")

            if prefix == "mod_verify":
                task_payload = {
                    "action": "verify",
                    "target_user_id": target_user_id,
                    "moderator_id": caller_id,
                    "moderator_name": caller_display,
                    "reason": "",
                    "source_message_id": source_message_id or None,
                    "application_id": str(
                        interaction.get("application_id") or settings.discord_app_id
                    ),
                    "interaction_token": str(interaction["token"]),
                }
                await _dispatch_task(
                    "/tasks/process-mod-action",
                    task_payload,
                    settings,
                    dispatcher,
                    discord,
                    verifier,
                )
                return deferred_ephemeral()

            if prefix == "mod_hc":
                return _build_mod_reason_modal(
                    custom_id=f"mod_hc_modal:{target_user_id}:{source_message_id}",
                    title="Grant Passed HC Role",
                    label="Reason (optional)",
                    description="Optional note sent to the user explaining why they received Passed HC.",
                    required=False,
                )

            if prefix == "mod_reject":
                return _build_mod_reason_modal(
                    custom_id=f"mod_reject_modal:{target_user_id}:{source_message_id}",
                    title="Reject Verification Request",
                    label="Rejection Reason",
                    description="Sent to the user so they know what to fix if they resubmit.",
                    required=True,
                )

        return ephemeral_message("Unrecognized button action.")

    # 3. Modal submissions
    if itype == INTERACTION_MODAL_SUBMIT:
        custom_id = str(data.get("custom_id") or "")
        inputs = extract_modal_inputs(data)

        if custom_id == "submit_proof_modal":
            if settings.role_verified in caller_roles:
                return ephemeral_message("You already have the **Verified** role! 🎉")

            file_ids = inputs.get("proof_file") or []
            if isinstance(file_ids, str):
                file_ids = [file_ids]

            resolved_attachments = (data.get("resolved") or {}).get("attachments") or {}
            attachments_meta: list[dict[str, Any]] = []
            for fid in file_ids[: settings.max_attachments]:
                att = resolved_attachments.get(str(fid))
                if not att:
                    continue
                size = int(att.get("size") or 0)
                if size > settings.max_attachment_bytes:
                    return ephemeral_message(
                        f"❌ `{att.get('filename', 'file')}` is too large (max 10 MB). "
                        "Please compress or screenshot it and try again."
                    )
                attachments_meta.append(
                    {
                        "id": str(att.get("id") or fid),
                        "filename": str(att.get("filename") or "proof.png"),
                        "url": str(att.get("url") or ""),
                        "content_type": att.get("content_type"),
                        "size": size,
                    }
                )

            if not attachments_meta:
                return ephemeral_message(
                    "❌ Please attach at least one screenshot or PDF and try again."
                )

            additional_info = str(inputs.get("additional_info") or "").strip()
            task_payload = {
                "application_id": str(interaction.get("application_id") or settings.discord_app_id),
                "interaction_token": str(interaction["token"]),
                "user_id": caller_id,
                "username": caller_username,
                "display_name": caller_display,
                "additional_info": additional_info,
                "attachments": attachments_meta,
            }
            await _dispatch_task(
                "/tasks/process-verification", task_payload, settings, dispatcher, discord, verifier
            )
            return deferred_ephemeral()

        if custom_id.startswith(("mod_hc_modal:", "mod_reject_modal:")):
            if not has_mod_permission(interaction):
                return ephemeral_message(
                    "You need the **Manage Roles** permission to perform this action."
                )

            parts = custom_id.split(":")
            if len(parts) < 2:
                return ephemeral_message("Invalid modal state.")
            action = "passed_hc" if parts[0] == "mod_hc_modal" else "reject"
            target_user_id = parts[1]
            source_message_id = parts[2] if len(parts) > 2 and parts[2] else None
            reason = str(inputs.get("reason") or "").strip()

            task_payload = {
                "action": action,
                "target_user_id": target_user_id,
                "moderator_id": caller_id,
                "moderator_name": caller_display,
                "reason": reason,
                "source_message_id": source_message_id,
                "application_id": str(interaction.get("application_id") or settings.discord_app_id),
                "interaction_token": str(interaction["token"]),
            }
            await _dispatch_task(
                "/tasks/process-mod-action", task_payload, settings, dispatcher, discord, verifier
            )
            return deferred_ephemeral()

        return ephemeral_message("Unrecognized modal submission.")

    return ephemeral_message("Unsupported interaction type.")
