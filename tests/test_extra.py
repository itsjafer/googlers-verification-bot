from __future__ import annotations

import json
import logging

from app.config import load_settings
from app.logging_utils import CloudRunJSONFormatter
from app.main import _extract_internal_secret
from app.verifier import VERIFICATION_SYSTEM_PROMPT, normalize_mime_type
from scripts.backfill_verifications import match_member_from_candidates, parse_legacy_message


def test_normalize_mime_type() -> None:
    assert normalize_mime_type("image/jpg; charset=utf-8", "x.jpg") == "image/jpeg"
    assert normalize_mime_type("application/pdf", "offer.pdf") == "application/pdf"
    assert normalize_mime_type(None, "Screenshot.PNG") == "image/png"
    assert normalize_mime_type(None, "photo.HEIC") == "image/heic"
    assert normalize_mime_type("application/octet-stream", "file.webp") == "image/webp"
    assert normalize_mime_type("text/plain", "notes.txt") == "application/octet-stream"


def test_verification_prompt_includes_key_policies() -> None:
    assert "PASSED_HC" in VERIFICATION_SYSTEM_PROMPT
    assert "MANUAL_REVIEW" in VERIFICATION_SYSTEM_PROMPT
    assert "@google.com" in VERIFICATION_SYSTEM_PROMPT
    assert "@xwf.google.com" in VERIFICATION_SYSTEM_PROMPT
    assert "UNTRUSTED" in VERIFICATION_SYSTEM_PROMPT
    assert "UNSIGNED" in VERIFICATION_SYSTEM_PROMPT


def test_extract_internal_secret_variants() -> None:
    assert _extract_internal_secret("Bearer abc123", None) == "abc123"
    assert _extract_internal_secret("bearer abc123 ", None) == "abc123"
    assert _extract_internal_secret(None, "xyz789") == "xyz789"
    assert _extract_internal_secret("Bearer old", "preferred") == "preferred"
    assert _extract_internal_secret(None, None) == ""


def test_cloud_run_json_formatter() -> None:
    fmt = CloudRunJSONFormatter()
    rec = logging.LogRecord(
        name="app.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    rec.user_id = "123456789012345678"
    parsed = json.loads(fmt.format(rec))
    assert parsed["severity"] == "WARNING"
    assert parsed["message"] == "hello world"
    assert parsed["user_id"] == "123456789012345678"


def test_load_settings_merges_packed_secret_json(monkeypatch) -> None:
    monkeypatch.setenv(
        "APP_SECRETS_JSON",
        json.dumps({"DISCORD_BOT_TOKEN": "tok-from-secret", "INTERNAL_FORM_SECRET": "int-sec"}),
    )
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("INTERNAL_FORM_SECRET", raising=False)
    cfg = load_settings()
    assert cfg.discord_bot_token == "tok-from-secret"
    assert cfg.internal_form_secret == "int-sec"


def test_parse_legacy_message_external_and_internal() -> None:
    # Header message without embed is skipped
    assert parse_legacy_message({"id": "1", "content": "instructions", "embeds": []}) is None

    # Modern v2 card with buttons is skipped
    assert (
        parse_legacy_message(
            {
                "id": "2",
                "embeds": [{"fields": [{"name": "username", "value": "alice"}]}],
                "components": [{"type": 1, "components": []}],
            }
        )
        is None
    )

    # Legacy external webhook message
    ext = parse_legacy_message(
        {
            "id": "100",
            "timestamp": "2026-11-20T10:00:00Z",
            "webhook_id": "897672968497430548",
            "embeds": [
                {
                    "description": (
                        "**Username**: alice_g\n"
                        "**Proof**: https://drive.google.com/uc?export=download&id=1DfoHRIU80IUpcdvfWzMVVLZq3-N-BX0J\n"
                        "**Additional Info**: Incoming L3\n"
                        "**Gemini Verdict**: FAILED"
                    ),
                    "image": {
                        "url": "https://drive.google.com/uc?export=download&id=1DfoHRIU80IUpcdvfWzMVVLZq3-N-BX0J"
                    },
                    "fields": [
                        {"name": "verdict", "value": "FAILED"},
                        {"name": "username", "value": "alice_g"},
                    ],
                }
            ],
        }
    )
    assert ext is not None
    assert ext.username == "alice_g"
    assert ext.drive_file_id == "1DfoHRIU80IUpcdvfWzMVVLZq3-N-BX0J"
    assert ext.additional_info == "Incoming L3"
    assert not ext.is_internal_form

    # Legacy internal corp form message
    internal = parse_legacy_message(
        {
            "id": "101",
            "timestamp": "2026-11-20T10:00:00Z",
            "webhook_id": "897672968497430548",
            "embeds": [
                {
                    "description": (
                        "**Username**: bob_g\n"
                        "**Proof**: [User verified using internal form]\n"
                        "**Additional Info**: [User verified using internal form]"
                    ),
                    "fields": [
                        {"name": "verdict", "value": "VERIFY"},
                        {"name": "username", "value": "bob_g"},
                    ],
                }
            ],
        }
    )
    assert internal is not None
    assert internal.is_internal_form
    assert internal.drive_file_id is None


def test_match_member_from_candidates_prefers_exact_username() -> None:
    cands = [
        {"user": {"id": "1", "username": "other", "global_name": "Alice"}, "nick": None},
        {"user": {"id": "2", "username": "alice", "global_name": "Alice G"}, "nick": None},
    ]
    matched = match_member_from_candidates("@Alice#0", cands)
    assert matched is not None
    assert matched["user"]["id"] == "2"


def test_verify_cloud_tasks_oidc_rejects_missing_or_bad_header() -> None:
    from app.config import Settings
    from app.tasks import verify_cloud_tasks_oidc

    cfg = Settings(
        service_url="https://example.run.app",
        task_service_account="verify-bot-sa@proj.iam.gserviceaccount.com",
        skip_task_auth=False,
    )
    assert not verify_cloud_tasks_oidc(None, cfg)
    assert not verify_cloud_tasks_oidc("Basic abc", cfg)
    assert not verify_cloud_tasks_oidc("Bearer ", cfg)


def test_verify_cloud_tasks_oidc_accepts_valid_claims(monkeypatch) -> None:
    from google.oauth2 import id_token

    from app.config import Settings
    from app.tasks import verify_cloud_tasks_oidc

    cfg = Settings(
        service_url="https://example.run.app",
        task_service_account="verify-bot-sa@proj.iam.gserviceaccount.com",
        skip_task_auth=False,
    )
    monkeypatch.setattr(
        id_token,
        "verify_oauth2_token",
        lambda tok, req, audience: {
            "email": "verify-bot-sa@proj.iam.gserviceaccount.com",
            "email_verified": True,
        },
    )
    assert verify_cloud_tasks_oidc("Bearer good-jwt", cfg)

    monkeypatch.setattr(
        id_token,
        "verify_oauth2_token",
        lambda tok, req, audience: {
            "email": "attacker@evil.iam.gserviceaccount.com",
            "email_verified": True,
        },
    )
    assert not verify_cloud_tasks_oidc("Bearer wrong-sa-jwt", cfg)
