from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
ALLOWED_ATTACHMENT_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})


def verify_discord_signature(
    public_key_hex: str,
    signature_hex: str | None,
    timestamp: str | None,
    raw_body: bytes,
) -> bool:
    if not signature_hex or not timestamp or not public_key_hex:
        return False
    try:
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
        verify_key.verify(timestamp.encode("utf-8") + raw_body, bytes.fromhex(signature_hex))
        return True
    except (ValueError, BadSignatureError):
        return False


class DiscordAPIError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Discord API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class DiscordClient:
    def __init__(self, bot_token: str, timeout: float = 20.0) -> None:
        self._bot_token = bot_token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        if not self._bot_token:
            raise RuntimeError("DISCORD_BOT_TOKEN is not configured")
        return {
            "Authorization": f"Bot {self._bot_token}",
            "User-Agent": "GooglersVerifyBot/2.0 (+https://discord.com)",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        files: list[tuple[str, bytes, str]] | None = None,
        use_auth: bool = True,
        max_retries: int = 3,
    ) -> httpx.Response:
        url = f"{DISCORD_API_BASE}{path}"
        headers = self._headers() if use_auth else {"User-Agent": "GooglersVerifyBot/2.0"}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for attempt in range(max_retries):
                if files:
                    multipart: list[tuple[str, Any]] = []
                    if json_body is not None:
                        multipart.append(
                            ("payload_json", (None, json.dumps(json_body), "application/json"))
                        )
                    for idx, (filename, data, content_type) in enumerate(files):
                        multipart.append((f"files[{idx}]", (filename, data, content_type)))
                    resp = await client.request(method, url, headers=headers, files=multipart)
                else:
                    resp = await client.request(method, url, headers=headers, json=json_body)

                if resp.status_code == 429 and attempt < max_retries - 1:
                    retry_after = 1.5
                    with contextlib.suppress(Exception):
                        retry_after = float(resp.json().get("retry_after", retry_after))
                    await asyncio.sleep(min(retry_after + 0.1, 10.0))
                    continue

                if resp.status_code >= 500 and attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

                return resp

        raise DiscordAPIError(500, f"Exhausted retries for {method} {path}")

    async def get_guild_member(self, guild_id: str, user_id: str) -> dict[str, Any] | None:
        resp = await self._request("GET", f"/guilds/{guild_id}/members/{user_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise DiscordAPIError(resp.status_code, resp.text)
        return resp.json()

    async def add_role(self, guild_id: str, user_id: str, role_id: str) -> None:
        resp = await self._request("PUT", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}")
        if resp.status_code >= 400:
            raise DiscordAPIError(resp.status_code, resp.text)

    async def remove_role(self, guild_id: str, user_id: str, role_id: str) -> None:
        resp = await self._request(
            "DELETE", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}"
        )
        # 404 when removing a role the user doesn't have is harmless
        if resp.status_code >= 400 and resp.status_code != 404:
            raise DiscordAPIError(resp.status_code, resp.text)

    async def update_member_roles(
        self,
        guild_id: str,
        user_id: str,
        add_role_id: str,
        remove_role_ids: list[str],
    ) -> None:
        await self.add_role(guild_id, user_id, add_role_id)
        for role_id in remove_role_ids:
            if role_id != add_role_id:
                await self.remove_role(guild_id, user_id, role_id)

    async def send_dm(
        self,
        user_id: str,
        content: str,
        embeds: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str | None]:
        dm_resp = await self._request(
            "POST", "/users/@me/channels", json_body={"recipient_id": user_id}
        )
        if dm_resp.status_code == 403:
            return False, "User has DMs disabled"
        if dm_resp.status_code >= 400:
            logger.warning("Failed creating DM channel for %s: %s", user_id, dm_resp.text)
            return False, f"DM channel error ({dm_resp.status_code})"

        channel_id = dm_resp.json()["id"]
        payload: dict[str, Any] = {"content": content}
        if embeds:
            payload["embeds"] = embeds

        msg_resp = await self._request(
            "POST", f"/channels/{channel_id}/messages", json_body=payload
        )
        if msg_resp.status_code == 403:
            return False, "User has DMs disabled"
        if msg_resp.status_code >= 400:
            logger.warning("Failed sending DM to %s: %s", user_id, msg_resp.text)
            return False, f"DM send error ({msg_resp.status_code})"
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
        payload: dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if embeds is not None:
            payload["embeds"] = embeds
        if components is not None:
            payload["components"] = components

        resp = await self._request(
            "POST", f"/channels/{channel_id}/messages", json_body=payload, files=files
        )
        if resp.status_code >= 400:
            raise DiscordAPIError(resp.status_code, resp.text)
        return resp.json()

    async def get_channel_message(self, channel_id: str, message_id: str) -> dict[str, Any] | None:
        resp = await self._request("GET", f"/channels/{channel_id}/messages/{message_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise DiscordAPIError(resp.status_code, resp.text)
        return resp.json()

    async def get_channel_messages(self, channel_id: str, limit: int = 25) -> list[dict[str, Any]]:
        resp = await self._request("GET", f"/channels/{channel_id}/messages?limit={limit}")
        if resp.status_code >= 400:
            raise DiscordAPIError(resp.status_code, resp.text)
        return resp.json()

    async def delete_channel_message(self, channel_id: str, message_id: str) -> bool:
        resp = await self._request("DELETE", f"/channels/{channel_id}/messages/{message_id}")
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            raise DiscordAPIError(resp.status_code, resp.text)
        return True

    async def edit_interaction_response(
        self,
        app_id: str,
        interaction_token: str,
        *,
        content: str | None = None,
        embeds: list[dict[str, Any]] | None = None,
        components: list[dict[str, Any]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if embeds is not None:
            payload["embeds"] = embeds
        if components is not None:
            payload["components"] = components

        resp = await self._request(
            "PATCH",
            f"/webhooks/{app_id}/{interaction_token}/messages/@original",
            json_body=payload,
            use_auth=False,
        )
        if resp.status_code >= 400:
            logger.error("Failed editing interaction response: %s %s", resp.status_code, resp.text)

    async def download_attachment(self, url: str, max_bytes: int = 10 * 1024 * 1024) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_ATTACHMENT_HOSTS:
            raise ValueError(f"Disallowed attachment host: {parsed.hostname}")

        chunks: list[bytes] = []
        total = 0
        async with (
            httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code >= 400:
                raise DiscordAPIError(resp.status_code, f"Failed downloading attachment: {url}")
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"Attachment exceeds max size of {max_bytes} bytes")
                chunks.append(chunk)
        return b"".join(chunks)
