from __future__ import annotations

import json
import os
from functools import lru_cache

from pydantic import BaseModel, Field


class Settings(BaseModel):
    # Discord identifiers (Googlers server + Gemini bot)
    discord_app_id: str = "1234557713262972949"
    discord_public_key: str = "ce43d8ffcd39ac0f3568be7bd222132550d0b4366e3aca908e7fbb369e35a5c8"
    discord_guild_id: str = "637161016412602369"

    role_verified: str = "650088540130115594"
    role_unverified: str = "978369828773564436"
    role_passed_hc: str = "904845166165245963"

    channel_verifications: str = "978516099425505280"
    channel_logs: str = "978515430111080459"

    # Optional pre-filled URL template for internal @google.com Google Form.
    # Example: "https://docs.google.com/forms/d/e/.../viewform?usp=pp_url&entry.123456789={user_id}"
    internal_form_url_template: str = ""

    # Secrets (loaded from APP_SECRETS_JSON or individual env vars)
    discord_bot_token: str = ""
    internal_form_secret: str = ""

    # GCP / Vertex AI / Cloud Tasks
    gcp_project: str = "discord-verify-bot-jafer"
    vertex_location: str = "global"
    gemini_model: str = "gemini-3.5-flash-lite"

    cloud_tasks_location: str = "us-central1"
    cloud_tasks_queue: str = "verification-queue"
    service_url: str = ""
    task_service_account: str = ""

    # Local dev / testing toggles
    inline_tasks: bool = False
    skip_task_auth: bool = False

    # Upload limits & TTLs
    max_attachment_bytes: int = Field(default=10 * 1024 * 1024)
    max_attachments: int = Field(default=2)
    auto_reject_ttl_hours: int = Field(default=48)


def load_settings() -> Settings:
    secrets_raw = os.environ.get("APP_SECRETS_JSON", "").strip()
    secrets: dict[str, str] = {}
    if secrets_raw:
        parsed = json.loads(secrets_raw)
        if isinstance(parsed, dict):
            secrets = {str(k): str(v) for k, v in parsed.items() if v is not None}

    def env(name: str, default: str = "") -> str:
        val = os.environ.get(name)
        if val is not None and val != "":
            return val
        if name in secrets and secrets[name] != "":
            return secrets[name]
        return default

    return Settings(
        discord_app_id=env("DISCORD_APP_ID", "1234557713262972949"),
        discord_public_key=env(
            "DISCORD_PUBLIC_KEY",
            "ce43d8ffcd39ac0f3568be7bd222132550d0b4366e3aca908e7fbb369e35a5c8",
        ),
        discord_guild_id=env("DISCORD_GUILD_ID", "637161016412602369"),
        role_verified=env("ROLE_VERIFIED", "650088540130115594"),
        role_unverified=env("ROLE_UNVERIFIED", "978369828773564436"),
        role_passed_hc=env("ROLE_PASSED_HC", "904845166165245963"),
        channel_verifications=env("CHANNEL_VERIFICATIONS", "978516099425505280"),
        channel_logs=env("CHANNEL_LOGS", "978515430111080459"),
        internal_form_url_template=env("INTERNAL_FORM_URL_TEMPLATE", ""),
        discord_bot_token=env("DISCORD_BOT_TOKEN", ""),
        internal_form_secret=env("INTERNAL_FORM_SECRET", ""),
        gcp_project=env("GCP_PROJECT", "discord-verify-bot-jafer"),
        vertex_location=env("VERTEX_LOCATION", "global"),
        gemini_model=env("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        cloud_tasks_location=env("CLOUD_TASKS_LOCATION", "us-central1"),
        cloud_tasks_queue=env("CLOUD_TASKS_QUEUE", "verification-queue"),
        service_url=env("SERVICE_URL", "").rstrip("/"),
        task_service_account=env("TASK_SERVICE_ACCOUNT", ""),
        inline_tasks=env("INLINE_TASKS", "false").lower() in ("1", "true", "yes"),
        skip_task_auth=env("SKIP_TASK_AUTH", "false").lower() in ("1", "true", "yes"),
        auto_reject_ttl_hours=int(env("AUTO_REJECT_TTL_HOURS", "48")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
