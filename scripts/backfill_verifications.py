#!/usr/bin/env python3
"""One-time (or ad-hoc) backfill tool for legacy webhook verification messages in #verifications.

Default mode is --dry-run (strictly read-only). Pass --apply to perform role updates,
re-post borderline items with modern 1-click mod buttons, archive logs, and clean up old cards.

Examples:
    python3 scripts/backfill_verifications.py --dry-run
    python3 scripts/backfill_verifications.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow running directly via `python3 scripts/backfill_verifications.py` without PYTHONPATH=.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from google import genai  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402

from app.config import Settings, load_settings  # noqa: E402
from app.discord_api import DiscordClient  # noqa: E402
from app.verifier import ProofVerifier, VerificationResult, normalize_mime_type  # noqa: E402
from app.workflows import (  # noqa: E402
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_WARNING,
    PASSED_HC_DM_BASE,
    PASSED_HC_DM_TAIL,
    VERIFIED_DM,
    _now_iso,
    _sanitize_filename,
    build_mod_buttons,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("backfill")

DRIVE_ID_RE = re.compile(r"(?:[?&]id=|/d/)([A-Za-z0-9_-]{20,})")
ADDITIONAL_INFO_RE = re.compile(
    r"\*\*Additional Info\*\*:\s*(.*?)(?:\n\*\*Gemini Verdict\*\*:|\Z)", re.DOTALL
)
INTERNAL_FORM_MARKER = "[User verified using internal form]"
LEGACY_WEBHOOK_ID = "897672968497430548"


@dataclass
class LegacyRequest:
    message_id: str
    timestamp: str
    username: str
    old_verdict: str
    drive_file_id: str | None
    is_internal_form: bool
    additional_info: str
    raw_description: str


def parse_legacy_message(msg: dict[str, Any]) -> LegacyRequest | None:
    """Parse a legacy webhook embed from #verifications. Returns None for non-legacy messages."""
    # Skip non-embed messages (e.g. channel instructions) and v2 interactive cards (have buttons)
    embeds = msg.get("embeds") or []
    if not embeds or msg.get("components"):
        return None
    if msg.get("webhook_id") and str(msg["webhook_id"]) != LEGACY_WEBHOOK_ID:
        return None

    emb = embeds[0]
    fields = {str(f.get("name", "")): str(f.get("value", "")) for f in emb.get("fields", [])}
    username = fields.get("username", "").strip()
    old_verdict = fields.get("verdict", "").strip()
    if not username:
        return None

    desc = str(emb.get("description") or "")
    img_url = str((emb.get("image") or {}).get("url") or "")
    is_internal = INTERNAL_FORM_MARKER in desc and old_verdict == "VERIFY"

    drive_match = DRIVE_ID_RE.search(img_url) or DRIVE_ID_RE.search(desc)
    drive_file_id = drive_match.group(1) if drive_match else None

    info_match = ADDITIONAL_INFO_RE.search(desc)
    additional_info = info_match.group(1).strip() if info_match else ""
    if additional_info == INTERNAL_FORM_MARKER:
        additional_info = ""

    return LegacyRequest(
        message_id=str(msg["id"]),
        timestamp=str(msg.get("timestamp", "")),
        username=username,
        old_verdict=old_verdict,
        drive_file_id=drive_file_id,
        is_internal_form=is_internal,
        additional_info=additional_info,
        raw_description=desc,
    )


def match_member_from_candidates(
    username: str, candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    cleaned = username.lstrip("@").split("#")[0].strip().lower()
    if not cleaned:
        return None
    # Exact match on username first, then global_name / nick
    for mem in candidates:
        usr = mem.get("user") or {}
        if (usr.get("username") or "").lower() == cleaned:
            return mem
    for mem in candidates:
        usr = mem.get("user") or {}
        if (usr.get("global_name") or "").lower() == cleaned or (
            mem.get("nick") or ""
        ).lower() == cleaned:
            return mem
    return None


async def search_guild_member(
    discord: DiscordClient, guild_id: str, username: str
) -> dict[str, Any] | None:
    query = username.lstrip("@").split("#")[0].strip()[:32]
    if not query:
        return None
    resp = await discord._request(
        "GET",
        f"/guilds/{guild_id}/members/search?query={httpx.QueryParams({'q': query})['q']}&limit=10",
    )
    if resp.status_code != 200:
        logger.warning("Member search failed for %s: HTTP %s", username, resp.status_code)
        return None
    return match_member_from_candidates(username, resp.json())


def download_drive_file(file_id: str, tmpdir: Path) -> tuple[str, bytes, str] | None:
    """Download a proof file from Google Drive using the authenticated `gdrive` CLI."""
    info_proc = subprocess.run(
        ["gdrive", "readonly", "info", file_id, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    filename = f"{file_id}.png"
    mime_type = "image/png"
    if info_proc.returncode == 0:
        try:
            meta = json.loads(info_proc.stdout)
            filename = meta.get("name") or filename
            mime_type = meta.get("mimeType") or mime_type
        except json.JSONDecodeError:
            pass

    out_path = tmpdir / _sanitize_filename(filename, 0)
    dl_proc = subprocess.run(
        ["gdrive", "readonly", "download", file_id, "--out", str(out_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if dl_proc.returncode != 0 or not out_path.exists():
        logger.warning("gdrive download failed for %s: %s", file_id, dl_proc.stderr.strip())
        return None

    data = out_path.read_bytes()
    norm_mime = normalize_mime_type(mime_type, filename)
    return _sanitize_filename(filename, 0), data, norm_mime


async def fetch_all_messages(
    discord: DiscordClient, channel_id: str, max_messages: int = 500
) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    before: str | None = None
    while len(msgs) < max_messages:
        path = f"/channels/{channel_id}/messages?limit=100"
        if before:
            path += f"&before={before}"
        resp = await discord._request("GET", path)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed fetching messages: HTTP {resp.status_code} {resp.text}")
        batch = resp.json()
        if not batch:
            break
        msgs.extend(batch)
        before = batch[-1]["id"]
        if len(batch) < 100:
            break
    return msgs


def _build_verifier_with_gcloud_adc(settings: Settings) -> ProofVerifier:
    verifier = ProofVerifier(settings)
    # Use gcloud access token when running from a developer workstation without local ADC file
    tok_proc = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=False,
    )
    if tok_proc.returncode == 0 and tok_proc.stdout.strip():
        creds = Credentials(token=tok_proc.stdout.strip())
        verifier._client = genai.Client(
            vertexai=True,
            project=settings.gcp_project,
            location=settings.vertex_location,
            credentials=creds,
        )
    return verifier


async def run_backfill(*, apply: bool, include_rejected: bool, limit: int | None) -> dict[str, Any]:
    settings = load_settings()
    if not settings.discord_bot_token:
        # Load from Secret Manager via gcloud when run locally
        sec_proc = subprocess.run(
            [
                "gcloud",
                "secrets",
                "versions",
                "access",
                "latest",
                "--secret=bot-secrets",
                f"--project={settings.gcp_project}",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        os.environ["APP_SECRETS_JSON"] = sec_proc.stdout.strip()
        settings = load_settings()

    discord = DiscordClient(settings.discord_bot_token)
    verifier = _build_verifier_with_gcloud_adc(settings)

    raw_messages = await fetch_all_messages(discord, settings.channel_verifications)
    # Process newest -> oldest so if a user's latest submission verifies them, older duplicates are cleaned up
    parsed: list[LegacyRequest] = []
    for m in raw_messages:
        req = parse_legacy_message(m)
        if req is None:
            continue
        if not include_rejected and req.old_verdict == "REJECT":
            continue
        parsed.append(req)

    if limit is not None:
        parsed = parsed[:limit]

    stats: dict[str, Any] = {
        "mode": "APPLY" if apply else "DRY_RUN",
        "total_candidates": len(parsed),
        "auto_verified": 0,
        "auto_verified_internal": 0,
        "auto_passed_hc": 0,
        "cleaned_already_verified": 0,
        "cleaned_already_passed_hc_dup": 0,
        "upgraded_to_mod_card": 0,
        "skipped_user_not_found": [],
        "skipped_download_failed": [],
        "errors": [],
    }

    # Track users we already verified during this run to clean up duplicates cleanly
    newly_verified_users: set[str] = set()
    newly_hc_users: set[str] = set()
    member_cache: dict[str, dict[str, Any] | None] = {}

    with tempfile.TemporaryDirectory(prefix="googlers_backfill_") as tmp_str:
        tmpdir = Path(tmp_str)

        for idx, req in enumerate(parsed, start=1):
            uname_key = req.username.lower()
            if uname_key not in member_cache:
                member_cache[uname_key] = await search_guild_member(
                    discord, settings.discord_guild_id, req.username
                )
            member = member_cache[uname_key]

            if member is None:
                stats["skipped_user_not_found"].append(
                    {
                        "message_id": req.message_id,
                        "username": req.username,
                        "old_verdict": req.old_verdict,
                    }
                )
                logger.info("[%d/%d] SKIP (not in server): %s", idx, len(parsed), req.username)
                continue

            user = member.get("user") or {}
            user_id = str(user["id"])
            display_name = str(
                member.get("nick")
                or user.get("global_name")
                or user.get("username")
                or req.username
            )
            roles = set(str(r) for r in (member.get("roles") or []))

            # 1. Already Verified -> clean up stale card
            if settings.role_verified in roles or user_id in newly_verified_users:
                stats["cleaned_already_verified"] += 1
                logger.info(
                    "[%d/%d] CLEANUP (already verified): %s (<@%s>)",
                    idx,
                    len(parsed),
                    req.username,
                    user_id,
                )
                if apply:
                    await discord.send_channel_message(
                        settings.channel_logs,
                        content=(
                            f"🧹 | Backfill cleaned stale verification card for <@{user_id}> "
                            f"(`{req.username}` — already holds `verified` role)."
                        ),
                    )
                    await discord.delete_channel_message(
                        settings.channel_verifications, req.message_id
                    )
                continue

            # 2. Internal @google.com form submission -> instant Verify
            if req.is_internal_form:
                stats["auto_verified_internal"] += 1
                newly_verified_users.add(user_id)
                logger.info(
                    "[%d/%d] VERIFY (internal form): %s (<@%s>)",
                    idx,
                    len(parsed),
                    req.username,
                    user_id,
                )
                if apply:
                    await discord.update_member_roles(
                        settings.discord_guild_id,
                        user_id,
                        settings.role_verified,
                        [settings.role_passed_hc, settings.role_unverified],
                    )
                    dm_ok, dm_err = await discord.send_dm(user_id, VERIFIED_DM)
                    dm_note = "" if dm_ok else f" *(DM skipped: {dm_err})*"
                    await discord.send_channel_message(
                        settings.channel_logs,
                        content=(
                            f"✅ | User <@{user_id}> updated roles: `+verified`, `-(passed HC, unverified)`. "
                            f"Processed by **backfill (internal @google.com form)**.{dm_note}"
                        ),
                        embeds=[
                            {
                                "title": "Backfill: Internal @google.com Form Verification",
                                "description": f"**User:** <@{user_id}> (`{req.username}` / `{display_name}`)",
                                "color": COLOR_SUCCESS,
                                "timestamp": _now_iso(),
                            }
                        ],
                    )
                    await discord.delete_channel_message(
                        settings.channel_verifications, req.message_id
                    )
                continue

            # 3. External submission with Drive proof -> download & evaluate with Vertex AI Gemini
            if not req.drive_file_id:
                stats["skipped_download_failed"].append(
                    {
                        "message_id": req.message_id,
                        "username": req.username,
                        "reason": "no_drive_id",
                    }
                )
                continue

            downloaded_file = download_drive_file(req.drive_file_id, tmpdir)
            if downloaded_file is None:
                stats["skipped_download_failed"].append(
                    {
                        "message_id": req.message_id,
                        "username": req.username,
                        "drive_id": req.drive_file_id,
                    }
                )
                logger.warning("[%d/%d] SKIP (download failed): %s", idx, len(parsed), req.username)
                continue

            filename, file_bytes, mime_type = downloaded_file
            if len(file_bytes) > settings.max_attachment_bytes:
                stats["skipped_download_failed"].append(
                    {
                        "message_id": req.message_id,
                        "username": req.username,
                        "reason": "file_too_large",
                    }
                )
                continue

            try:
                result: VerificationResult = verifier.evaluate(
                    [(filename, file_bytes, mime_type)],
                    additional_info=req.additional_info,
                )
            except Exception as exc:
                logger.exception(
                    "[%d/%d] Vertex AI error for %s: %s", idx, len(parsed), req.username, exc
                )
                stats["errors"].append(
                    {"message_id": req.message_id, "username": req.username, "error": str(exc)}
                )
                continue

            first_image = filename if mime_type.startswith("image/") else None

            if result.confidence == "HIGH" and result.verdict == "VERIFY":
                stats["auto_verified"] += 1
                newly_verified_users.add(user_id)
                logger.info(
                    "[%d/%d] AUTO-VERIFY: %s (<@%s>) [%s] - %s",
                    idx,
                    len(parsed),
                    req.username,
                    user_id,
                    result.proof_type,
                    result.reason,
                )
                if apply:
                    await discord.update_member_roles(
                        settings.discord_guild_id,
                        user_id,
                        settings.role_verified,
                        [settings.role_passed_hc, settings.role_unverified],
                    )
                    dm_ok, dm_err = await discord.send_dm(user_id, VERIFIED_DM)
                    dm_note = "" if dm_ok else f" *(DM skipped: {dm_err})*"
                    embed: dict[str, Any] = {
                        "title": "Backfill Auto-Verification: VERIFY",
                        "description": (
                            f"**User:** <@{user_id}> (`{req.username}` / `{display_name}`)\n"
                            f"**Additional Info:** {req.additional_info or '_None_'}\n"
                            f"**Gemini Notes:** {result.reason}"
                        ),
                        "color": COLOR_SUCCESS,
                        "fields": [
                            {"name": "Verdict", "value": result.verdict, "inline": True},
                            {"name": "Confidence", "value": result.confidence, "inline": True},
                            {"name": "Proof Type", "value": result.proof_type, "inline": True},
                            {"name": "User ID", "value": user_id, "inline": True},
                        ],
                        "timestamp": _now_iso(),
                    }
                    if first_image:
                        embed["image"] = {"url": f"attachment://{first_image}"}
                    await discord.send_channel_message(
                        settings.channel_logs,
                        content=(
                            f"✅ | User <@{user_id}> updated roles: `+verified`, `-(passed HC, unverified)`. "
                            f"Processed by **backfill automatic verification**.{dm_note}"
                        ),
                        embeds=[embed],
                        files=[(filename, file_bytes, mime_type)],
                    )
                    await discord.delete_channel_message(
                        settings.channel_verifications, req.message_id
                    )
                continue

            if result.confidence == "HIGH" and result.verdict == "PASSED_HC":
                if settings.role_passed_hc in roles or user_id in newly_hc_users:
                    stats["cleaned_already_passed_hc_dup"] += 1
                    logger.info(
                        "[%d/%d] CLEANUP (already Passed HC): %s (<@%s>)",
                        idx,
                        len(parsed),
                        req.username,
                        user_id,
                    )
                    if apply:
                        await discord.send_channel_message(
                            settings.channel_logs,
                            content=(
                                f"🧹 | Backfill cleaned duplicate Passed HC card for <@{user_id}> "
                                f"(`{req.username}` — already holds `Passed Interviews` role)."
                            ),
                        )
                        await discord.delete_channel_message(
                            settings.channel_verifications, req.message_id
                        )
                    continue

                stats["auto_passed_hc"] += 1
                newly_hc_users.add(user_id)
                logger.info(
                    "[%d/%d] AUTO-PASSED_HC: %s (<@%s>) [%s] - %s",
                    idx,
                    len(parsed),
                    req.username,
                    user_id,
                    result.proof_type,
                    result.reason,
                )
                if apply:
                    await discord.update_member_roles(
                        settings.discord_guild_id,
                        user_id,
                        settings.role_passed_hc,
                        [settings.role_verified, settings.role_unverified],
                    )
                    dm_ok, dm_err = await discord.send_dm(
                        user_id, PASSED_HC_DM_BASE + PASSED_HC_DM_TAIL
                    )
                    dm_note = "" if dm_ok else f" *(DM skipped: {dm_err})*"
                    embed = {
                        "title": "Backfill Auto-Verification: PASSED_HC",
                        "description": (
                            f"**User:** <@{user_id}> (`{req.username}` / `{display_name}`)\n"
                            f"**Additional Info:** {req.additional_info or '_None_'}\n"
                            f"**Gemini Notes:** {result.reason}"
                        ),
                        "color": COLOR_WARNING,
                        "fields": [
                            {"name": "Verdict", "value": result.verdict, "inline": True},
                            {"name": "Confidence", "value": result.confidence, "inline": True},
                            {"name": "Proof Type", "value": result.proof_type, "inline": True},
                            {"name": "User ID", "value": user_id, "inline": True},
                        ],
                        "timestamp": _now_iso(),
                    }
                    if first_image:
                        embed["image"] = {"url": f"attachment://{first_image}"}
                    await discord.send_channel_message(
                        settings.channel_logs,
                        content=(
                            f"🟡 | User <@{user_id}> updated roles: `+passed HC`, `-(verified, unverified)`. "
                            f"Processed by **backfill automatic verification**.{dm_note}"
                        ),
                        embeds=[embed],
                        files=[(filename, file_bytes, mime_type)],
                    )
                    await discord.delete_channel_message(
                        settings.channel_verifications, req.message_id
                    )
                continue

            # Borderline / MANUAL_REVIEW -> replace legacy card with modern 1-click mod card
            stats["upgraded_to_mod_card"] += 1
            logger.info(
                "[%d/%d] MANUAL_REVIEW -> modern card: %s (<@%s>) [%s/%s] - %s",
                idx,
                len(parsed),
                req.username,
                user_id,
                result.verdict,
                result.confidence,
                result.reason,
            )
            if apply:
                review_embed: dict[str, Any] = {
                    "title": "Verification Request (Backfill — Manual Review)",
                    "description": (
                        f"**User:** <@{user_id}> (`{req.username}` / `{display_name}`)\n"
                        f"**Additional Info:** {req.additional_info or '_None_'}\n"
                        f"**Gemini Assessment:** {result.reason}"
                    ),
                    "color": COLOR_INFO,
                    "fields": [
                        {"name": "Suggested Verdict", "value": result.verdict, "inline": True},
                        {"name": "Confidence", "value": result.confidence, "inline": True},
                        {"name": "Proof Type", "value": result.proof_type, "inline": True},
                        {"name": "User ID", "value": user_id, "inline": True},
                    ],
                    "timestamp": req.timestamp or _now_iso(),
                }
                if first_image:
                    review_embed["image"] = {"url": f"attachment://{first_image}"}
                await discord.send_channel_message(
                    settings.channel_verifications,
                    embeds=[review_embed],
                    components=build_mod_buttons(user_id),
                    files=[(filename, file_bytes, mime_type)],
                )
                await discord.delete_channel_message(settings.channel_verifications, req.message_id)

            # Gentle pacing to stay well within Discord REST limits
            await asyncio.sleep(0.15)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill legacy verification messages in #verifications."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply role updates and clean up messages (default is dry-run).",
    )
    parser.add_argument(
        "--include-rejected",
        action="store_true",
        default=True,
        help="Also upgrade/re-evaluate legacy REJECT messages (default: true).",
    )
    parser.add_argument("--skip-rejected", action="store_true", help="Skip legacy REJECT messages.")
    parser.add_argument(
        "--limit", type=int, default=None, help="Limit number of messages processed."
    )
    args = parser.parse_args()

    stats = asyncio.run(
        run_backfill(
            apply=args.apply,
            include_rejected=not args.skip_rejected,
            limit=args.limit,
        )
    )
    print("\n=== BACKFILL SUMMARY ===")
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
