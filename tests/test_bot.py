from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

from app.config import Settings
from app.main import create_app
from app.verifier import VerificationResult


class FakeDiscordClient:
    def __init__(self) -> None:
        self.role_updates: list[tuple[str, str, str, list[str]]] = []
        self.dms: list[tuple[str, str]] = []
        self.channel_messages: list[dict[str, Any]] = []
        self.deleted_messages: list[tuple[str, str]] = []
        self.edited_responses: list[dict[str, Any]] = []
        self.members: dict[str, dict[str, Any]] = {
            "111111111111111111": {
                "user": {"id": "111111111111111111"},
                "roles": ["978369828773564436"],
            }
        }
        self.stored_messages: dict[str, dict[str, Any]] = {}
        self.dm_should_fail = False
        self.downloaded_bytes = b"\x89PNG\r\n\x1a\nfake-png-bytes"

    async def get_guild_member(self, guild_id: str, user_id: str) -> dict[str, Any] | None:
        return self.members.get(user_id)

    async def update_member_roles(
        self, guild_id: str, user_id: str, add_role_id: str, remove_role_ids: list[str]
    ) -> None:
        self.role_updates.append((guild_id, user_id, add_role_id, remove_role_ids))

    async def send_dm(
        self, user_id: str, content: str, embeds: list[dict[str, Any]] | None = None
    ) -> tuple[bool, str | None]:
        if self.dm_should_fail:
            return False, "User has DMs disabled"
        self.dms.append((user_id, content))
        return True, None

    async def send_channel_message(
        self,
        channel_id: str,
        *,
        content: str | None = None,
        embeds: list[dict[str, Any]] | None = None,
        components: list[dict[str, Any]] | None = None,
        files: list[tuple[str, bytes, str]] | None = None,
    ) -> dict[str, Any]:
        msg = {
            "id": f"msg_{len(self.channel_messages) + 1}",
            "channel_id": channel_id,
            "content": content,
            "embeds": embeds or [],
            "components": components or [],
            "files": files or [],
        }
        self.channel_messages.append(msg)
        return msg

    async def get_channel_message(self, channel_id: str, message_id: str) -> dict[str, Any] | None:
        return self.stored_messages.get(message_id)

    async def get_channel_messages(self, channel_id: str, limit: int = 25) -> list[dict[str, Any]]:
        return list(self.stored_messages.values())

    async def delete_channel_message(self, channel_id: str, message_id: str) -> bool:
        self.deleted_messages.append((channel_id, message_id))
        return self.stored_messages.pop(message_id, None) is not None

    async def edit_interaction_response(
        self,
        app_id: str,
        interaction_token: str,
        *,
        content: str | None = None,
        embeds: list[dict[str, Any]] | None = None,
        components: list[dict[str, Any]] | None = None,
    ) -> None:
        self.edited_responses.append(
            {"app_id": app_id, "token": interaction_token, "content": content}
        )

    async def download_attachment(self, url: str, max_bytes: int = 10 * 1024 * 1024) -> bytes:
        return self.downloaded_bytes


class FakeVerifier:
    def __init__(self, result: VerificationResult | Exception) -> None:
        self._result = result

    def evaluate(
        self, attachments: list[tuple[str, bytes, str]], additional_info: str = ""
    ) -> VerificationResult:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any], int]] = []

    def enqueue(self, path: str, payload: dict[str, Any], *, delay_seconds: int = 0) -> str:
        self.enqueued.append((path, payload, delay_seconds))
        return f"fake-task-{len(self.enqueued)}"


def _signed_headers(
    signing_key: SigningKey, raw_body: bytes, ts: str = "1700000000"
) -> dict[str, str]:
    sig = signing_key.sign(ts.encode("utf-8") + raw_body).signature.hex()
    return {
        "X-Signature-Ed25519": sig,
        "X-Signature-Timestamp": ts,
        "Content-Type": "application/json",
    }


def _make_app(
    verifier_result: VerificationResult | Exception | None = None,
    internal_form_url_template: str = "https://docs.google.com/forms/d/e/test/viewform?usp=pp_url&entry.123={user_id}",
) -> tuple[TestClient, SigningKey, FakeDiscordClient, Settings]:
    client, sk, fake_discord, settings, _ = _make_app_with_dispatcher(
        verifier_result=verifier_result,
        internal_form_url_template=internal_form_url_template,
    )
    return client, sk, fake_discord, settings


def _make_app_with_dispatcher(
    verifier_result: VerificationResult | Exception | None = None,
    internal_form_url_template: str = "https://docs.google.com/forms/d/e/test/viewform?usp=pp_url&entry.123={user_id}",
) -> tuple[TestClient, SigningKey, FakeDiscordClient, Settings, FakeDispatcher]:
    sk = SigningKey.generate()
    settings = Settings(
        discord_public_key=sk.verify_key.encode().hex(),
        discord_bot_token="fake-bot-token",
        internal_form_secret="super-secret-internal-token",
        internal_form_url_template=internal_form_url_template,
        inline_tasks=True,
        skip_task_auth=True,
    )
    fake_discord = FakeDiscordClient()
    fake_dispatcher = FakeDispatcher()
    fake_verifier = FakeVerifier(
        verifier_result
        or VerificationResult(
            verdict="VERIFY",
            confidence="HIGH",
            proof_type="signed_offer",
            reason="DocuSign completion confirmation for Google offer.",
        )
    )
    app = create_app(
        settings=settings,
        discord=fake_discord,
        verifier=fake_verifier,
        dispatcher=fake_dispatcher,
    )
    return TestClient(app), sk, fake_discord, settings, fake_dispatcher


def test_ping_signature_verification() -> None:
    client, sk, _, _ = _make_app()
    body = json.dumps({"type": 1}).encode()

    # Invalid signature rejected
    bad = client.post(
        "/interactions",
        content=body,
        headers={"X-Signature-Ed25519": "00" * 64, "X-Signature-Timestamp": "123"},
    )
    assert bad.status_code == 401

    # Valid signature returns PONG
    good = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert good.status_code == 200
    assert good.json() == {"type": 1}


def test_open_proof_modal_returns_file_upload_modal() -> None:
    client, sk, _, settings = _make_app()
    payload = {
        "type": 3,
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "111111111111111111", "username": "alice"},
            "roles": [settings.role_unverified],
        },
        "data": {"custom_id": "open_proof_modal"},
    }
    body = json.dumps(payload).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == 9
    assert data["data"]["custom_id"] == "submit_proof_modal"
    comps = data["data"]["components"]
    assert comps[0]["type"] == 18
    assert comps[0]["component"]["type"] == 19
    assert comps[1]["type"] == 18
    assert comps[1]["component"]["type"] == 4


def test_already_verified_user_gets_ephemeral_notice() -> None:
    client, sk, _, settings = _make_app()
    payload = {
        "type": 3,
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "111111111111111111", "username": "alice"},
            "roles": [settings.role_verified],
        },
        "data": {"custom_id": "open_proof_modal"},
    }
    body = json.dumps(payload).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert resp.status_code == 200
    assert resp.json()["type"] == 4
    assert "already have the **Verified** role" in resp.json()["data"]["content"]


def test_open_internal_link_prefills_discord_user_id() -> None:
    client, sk, _, settings = _make_app()
    payload = {
        "type": 3,
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "111111111111111111", "username": "alice"},
            "roles": [settings.role_unverified],
        },
        "data": {"custom_id": "open_internal_link"},
    }
    body = json.dumps(payload).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert resp.status_code == 200
    j = resp.json()
    assert j["type"] == 4
    link_btn = j["data"]["components"][0]["components"][0]
    assert link_btn["style"] == 5
    assert link_btn["url"].endswith("entry.123=111111111111111111")


def test_modal_submit_high_confidence_auto_verifies() -> None:
    client, sk, discord, settings = _make_app(
        VerificationResult(
            verdict="VERIFY",
            confidence="HIGH",
            proof_type="onboarding_portal",
            reason="Screenshot clearly shows Google onboarding portal.",
        )
    )
    payload = {
        "type": 5,
        "application_id": settings.discord_app_id,
        "token": "tok_123",
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "111111111111111111", "username": "alice"},
            "roles": [settings.role_unverified],
        },
        "data": {
            "custom_id": "submit_proof_modal",
            "components": [
                {
                    "type": 18,
                    "component": {"type": 19, "custom_id": "proof_file", "values": ["att_1"]},
                },
                {
                    "type": 18,
                    "component": {
                        "type": 4,
                        "custom_id": "additional_info",
                        "value": "New grad SWE",
                    },
                },
            ],
            "resolved": {
                "attachments": {
                    "att_1": {
                        "id": "att_1",
                        "filename": "onboarding.png",
                        "content_type": "image/png",
                        "size": 12345,
                        "url": "https://cdn.discordapp.com/attachments/1/2/onboarding.png",
                    }
                }
            },
        },
    }
    body = json.dumps(payload).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert resp.status_code == 200
    assert resp.json()["type"] == 5  # Deferred ephemeral

    assert len(discord.role_updates) == 1
    assert discord.role_updates[0] == (
        settings.discord_guild_id,
        "111111111111111111",
        settings.role_verified,
        [settings.role_passed_hc, settings.role_unverified],
    )
    assert len(discord.channel_messages) == 1
    assert discord.channel_messages[0]["channel_id"] == settings.channel_logs
    assert "You're verified" in discord.edited_responses[0]["content"]


def test_modal_submit_bar_not_met_auto_rejects_with_reason_and_48h_ttl() -> None:
    client, sk, discord, settings, dispatcher = _make_app_with_dispatcher(
        VerificationResult(
            verdict="REJECT",
            confidence="HIGH",
            proof_type="insufficient_or_unclear",
            reason="Email mentions Google but acceptance wording is slightly cropped.",
        )
    )
    payload = {
        "type": 5,
        "application_id": settings.discord_app_id,
        "token": "tok_456",
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "111111111111111111", "username": "alice"},
            "roles": [settings.role_unverified],
        },
        "data": {
            "custom_id": "submit_proof_modal",
            "components": [
                {
                    "type": 18,
                    "component": {"type": 19, "custom_id": "proof_file", "values": ["att_1"]},
                },
                {"type": 18, "component": {"type": 4, "custom_id": "additional_info", "value": ""}},
            ],
            "resolved": {
                "attachments": {
                    "att_1": {
                        "id": "att_1",
                        "filename": "email.png",
                        "content_type": "image/png",
                        "size": 5000,
                        "url": "https://cdn.discordapp.com/attachments/1/2/email.png",
                    }
                }
            },
        },
    }
    body = json.dumps(payload).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert resp.status_code == 200

    # User notified via both ephemeral reply and DM with Gemini's justification
    assert "acceptance wording is slightly cropped" in discord.edited_responses[0]["content"]
    assert "48 hours" in discord.edited_responses[0]["content"]
    assert len(discord.dms) == 1
    assert "acceptance wording is slightly cropped" in discord.dms[0][1]

    # Logged to #verifications-logs AND posted to #verifications for 48h mod override
    assert len(discord.channel_messages) == 2
    assert discord.channel_messages[0]["channel_id"] == settings.channel_logs
    override_msg = discord.channel_messages[1]
    assert override_msg["channel_id"] == settings.channel_verifications
    btn_ids = [b["custom_id"] for b in override_msg["components"][0]["components"]]
    assert btn_ids == [
        "mod_verify:111111111111111111",
        "mod_hc:111111111111111111",
        "mod_reject:111111111111111111",
        "mod_dismiss:111111111111111111",
    ]

    # Scheduled 48h Cloud Task to auto-delete the #verifications card
    assert len(dispatcher.enqueued) == 1
    task_path, task_payload, delay = dispatcher.enqueued[0]
    assert task_path == "/tasks/expire-verification"
    assert delay == 48 * 3600
    assert task_payload["message_id"] == override_msg["id"]


def test_auto_reject_preserves_existing_passed_hc_role() -> None:
    client, sk, discord, settings, _ = _make_app_with_dispatcher(
        VerificationResult(
            verdict="VERIFY",
            confidence="MEDIUM",
            proof_type="recruiter_acceptance_email",
            reason="Screenshot does not clearly confirm offer signature yet.",
        )
    )
    # User already holds Passed Interviews role
    discord.members["111111111111111111"]["roles"] = [settings.role_passed_hc]

    payload = {
        "type": 5,
        "application_id": settings.discord_app_id,
        "token": "tok_hc_keep",
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "111111111111111111", "username": "alice"},
            "roles": [settings.role_passed_hc],
        },
        "data": {
            "custom_id": "submit_proof_modal",
            "components": [
                {
                    "type": 18,
                    "component": {"type": 19, "custom_id": "proof_file", "values": ["att_1"]},
                },
            ],
            "resolved": {
                "attachments": {
                    "att_1": {
                        "id": "att_1",
                        "filename": "email.png",
                        "content_type": "image/png",
                        "size": 5000,
                        "url": "https://cdn.discordapp.com/attachments/1/2/email.png",
                    }
                }
            },
        },
    }
    body = json.dumps(payload).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert resp.status_code == 200
    # Must NOT downgrade existing Passed Interviews member back to unverified
    assert discord.role_updates == []
    assert "does not clearly confirm offer signature" in discord.dms[0][1]


def test_mod_dismiss_and_48h_expiry_endpoint() -> None:
    client, sk, discord, settings = _make_app()
    discord.stored_messages["override_msg_1"] = {"id": "override_msg_1", "embeds": []}
    discord.stored_messages["override_msg_2"] = {"id": "override_msg_2", "embeds": []}

    # 1. Mod clicks Dismiss -> deletes card immediately, no DM, no role change
    dismiss_payload = {
        "type": 3,
        "application_id": settings.discord_app_id,
        "token": "dismiss_tok",
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "333333333333333333", "username": "modjafer"},
            "permissions": str(1 << 28),
            "roles": [],
        },
        "message": {"id": "override_msg_1"},
        "data": {"custom_id": "mod_dismiss:111111111111111111"},
    }
    body = json.dumps(dismiss_payload).encode()
    r1 = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert r1.status_code == 200
    assert discord.role_updates == []
    assert discord.dms == []
    assert (settings.channel_verifications, "override_msg_1") in discord.deleted_messages
    assert "Dismissed" in discord.edited_responses[0]["content"]

    # 2. 48h TTL task fires for an unhandled card -> deletes it
    r2 = client.post(
        "/tasks/expire-verification",
        json={
            "channel_id": settings.channel_verifications,
            "message_id": "override_msg_2",
            "user_id": "111111111111111111",
        },
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "expired_deleted"
    assert (settings.channel_verifications, "override_msg_2") in discord.deleted_messages

    # 3. 48h TTL task fires for already-dismissed card -> idempotent no-op
    r3 = client.post(
        "/tasks/expire-verification",
        json={
            "channel_id": settings.channel_verifications,
            "message_id": "override_msg_1",
            "user_id": "111111111111111111",
        },
    )
    assert r3.status_code == 200
    assert r3.json()["status"] == "already_handled"


def test_modal_submit_ai_error_falls_back_gracefully_to_manual_review() -> None:
    client, sk, discord, settings = _make_app(RuntimeError("Vertex temporary outage"))
    payload = {
        "type": 5,
        "application_id": settings.discord_app_id,
        "token": "tok_err",
        "guild_id": settings.discord_guild_id,
        "member": {"user": {"id": "111111111111111111", "username": "alice"}, "roles": []},
        "data": {
            "custom_id": "submit_proof_modal",
            "components": [
                {
                    "type": 18,
                    "component": {"type": 19, "custom_id": "proof_file", "values": ["att_1"]},
                },
            ],
            "resolved": {
                "attachments": {
                    "att_1": {
                        "id": "att_1",
                        "filename": "proof.pdf",
                        "content_type": "application/pdf",
                        "size": 9000,
                        "url": "https://cdn.discordapp.com/attachments/1/2/proof.pdf",
                    }
                }
            },
        },
    }
    body = json.dumps(payload).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert resp.status_code == 200
    assert discord.channel_messages[0]["channel_id"] == settings.channel_verifications
    assert "Automated review failed" in discord.channel_messages[0]["embeds"][0]["description"]


def test_mod_verify_button_requires_permission_and_archives_card() -> None:
    client, sk, discord, settings = _make_app()
    discord.stored_messages["pending_999"] = {
        "id": "pending_999",
        "embeds": [{"title": "Verification Request", "description": "<@111111111111111111>"}],
        "attachments": [
            {
                "filename": "proof.png",
                "content_type": "image/png",
                "url": "https://cdn.discordapp.com/attachments/1/2/proof.png",
            }
        ],
    }

    # Non-mod blocked
    non_mod_payload = {
        "type": 3,
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "222222222222222222", "username": "nonmod"},
            "permissions": "0",
            "roles": [],
        },
        "message": {"id": "pending_999"},
        "data": {"custom_id": "mod_verify:111111111111111111"},
    }
    body = json.dumps(non_mod_payload).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert resp.status_code == 200
    assert "Manage Roles" in resp.json()["data"]["content"]
    assert discord.role_updates == []

    # Mod succeeds even when user has DMs disabled
    discord.dm_should_fail = True
    mod_payload = {
        "type": 3,
        "application_id": settings.discord_app_id,
        "token": "mod_tok",
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "333333333333333333", "username": "modjafer"},
            "permissions": str(1 << 28),
            "roles": [],
        },
        "message": {"id": "pending_999"},
        "data": {"custom_id": "mod_verify:111111111111111111"},
    }
    body = json.dumps(mod_payload).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert resp.status_code == 200
    assert len(discord.role_updates) == 1
    assert discord.deleted_messages == [(settings.channel_verifications, "pending_999")]
    assert discord.channel_messages[0]["channel_id"] == settings.channel_logs
    assert "could not DM user" in discord.edited_responses[0]["content"]


def test_internal_verify_endpoint_auth_and_flow() -> None:
    client, _, discord, settings = _make_app()

    # Wrong token rejected
    r_bad = client.post(
        "/internal-verify",
        json={"user_id": "111111111111111111"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r_bad.status_code == 403

    # Invalid snowflake rejected
    r_inv = client.post(
        "/internal-verify",
        json={"user_id": "not-a-snowflake"},
        headers={"Authorization": "Bearer super-secret-internal-token"},
    )
    assert r_inv.status_code == 400

    # Valid internal verification succeeds
    r_ok = client.post(
        "/internal-verify",
        json={"user_id": "111111111111111111", "notes": "SWE L4"},
        headers={"Authorization": "Bearer super-secret-internal-token"},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["status"] == "verified"
    assert discord.role_updates[0][2] == settings.role_verified
    assert discord.channel_messages[0]["channel_id"] == settings.channel_logs


def test_mod_reject_button_opens_modal_and_submit_rejects_and_idempotent() -> None:
    client, sk, discord, settings = _make_app()
    discord.stored_messages["pending_555"] = {
        "id": "pending_555",
        "embeds": [{"title": "Verification Request", "description": "<@111111111111111111>"}],
        "attachments": [],
    }

    # Click Reject button -> returns Modal
    btn_payload = {
        "type": 3,
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "333333333333333333", "username": "modjafer"},
            "permissions": str(1 << 28),
            "roles": [],
        },
        "message": {"id": "pending_555"},
        "data": {"custom_id": "mod_reject:111111111111111111"},
    }
    body = json.dumps(btn_payload).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(sk, body))
    assert resp.status_code == 200
    assert resp.json()["type"] == 9
    assert resp.json()["data"]["custom_id"] == "mod_reject_modal:111111111111111111:pending_555"

    # Submit Reject modal -> rejects, DMs reason, logs, deletes card
    modal_payload = {
        "type": 5,
        "application_id": settings.discord_app_id,
        "token": "rej_tok",
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "333333333333333333", "username": "modjafer"},
            "permissions": str(1 << 28),
            "roles": [],
        },
        "data": {
            "custom_id": "mod_reject_modal:111111111111111111:pending_555",
            "components": [
                {
                    "type": 18,
                    "component": {"type": 4, "custom_id": "reason", "value": "Blurry screenshot"},
                }
            ],
        },
    }
    body2 = json.dumps(modal_payload).encode()
    resp2 = client.post("/interactions", content=body2, headers=_signed_headers(sk, body2))
    assert resp2.status_code == 200
    assert discord.role_updates[0][2] == settings.role_unverified
    assert "Blurry screenshot" in discord.dms[0][1]
    assert discord.deleted_messages == [(settings.channel_verifications, "pending_555")]

    # Second click on already-deleted card is idempotent (no duplicate DM/role update)
    resp3 = client.post("/interactions", content=body2, headers=_signed_headers(sk, body2))
    assert resp3.status_code == 200
    assert len(discord.role_updates) == 1
    assert "already handled" in discord.edited_responses[-1]["content"]


def test_slash_command_verify_and_setup_panel() -> None:
    client, sk, discord, settings = _make_app()
    # /setup_panel posts panel
    setup_payload = {
        "type": 2,
        "guild_id": settings.discord_guild_id,
        "channel_id": "999999999999999999",
        "member": {
            "user": {"id": "333333333333333333", "username": "modjafer"},
            "permissions": str(1 << 28),
            "roles": [],
        },
        "data": {"name": "setup_panel"},
    }
    b1 = json.dumps(setup_payload).encode()
    r1 = client.post("/interactions", content=b1, headers=_signed_headers(sk, b1))
    assert r1.status_code == 200
    assert "panel posted" in r1.json()["data"]["content"]
    assert discord.channel_messages[0]["channel_id"] == "999999999999999999"

    # /passed_hc slash command
    hc_payload = {
        "type": 2,
        "application_id": settings.discord_app_id,
        "token": "hc_tok",
        "guild_id": settings.discord_guild_id,
        "member": {
            "user": {"id": "333333333333333333", "username": "modjafer"},
            "permissions": str(1 << 28),
            "roles": [],
        },
        "data": {
            "name": "passed_hc",
            "options": [
                {"name": "user", "value": "111111111111111111"},
                {"name": "reason", "value": "Unsigned offer"},
            ],
        },
    }
    b2 = json.dumps(hc_payload).encode()
    r2 = client.post("/interactions", content=b2, headers=_signed_headers(sk, b2))
    assert r2.status_code == 200
    assert discord.role_updates[0][2] == settings.role_passed_hc
    assert "Unsigned offer" in discord.dms[0][1]


@pytest.mark.anyio
async def test_download_attachment_blocks_ssrf() -> None:
    from app.discord_api import DiscordClient

    dc = DiscordClient("tok")
    with pytest.raises(ValueError, match="Disallowed attachment host"):
        await dc.download_attachment("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(ValueError, match="Disallowed attachment host"):
        await dc.download_attachment("https://evil.example.com/proof.png")
