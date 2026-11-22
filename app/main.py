from __future__ import annotations

import hmac
import json
import logging
import re
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.discord_api import DiscordClient, verify_discord_signature
from app.interactions import handle_interaction
from app.logging_utils import configure_logging
from app.tasks import TaskDispatcher, verify_cloud_tasks_oidc
from app.verifier import ProofVerifier
from app.workflows import (
    process_internal_verification,
    process_mod_action_task,
    process_verification_task,
)

configure_logging()
logger = logging.getLogger(__name__)

SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")


class InternalVerifyRequest(BaseModel):
    user_id: str
    notes: str = ""


def _extract_internal_secret(
    authorization: str | None,
    x_internal_secret: str | None,
) -> str:
    if x_internal_secret and x_internal_secret.strip():
        return x_internal_secret.strip()
    if not authorization:
        return ""
    auth = authorization.strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth


def create_app(
    settings: Settings | None = None,
    discord: DiscordClient | None = None,
    verifier: ProofVerifier | None = None,
    dispatcher: TaskDispatcher | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    discord_client = discord or DiscordClient(cfg.discord_bot_token)
    proof_verifier = verifier or ProofVerifier(cfg)
    task_dispatcher = dispatcher or TaskDispatcher(cfg)

    app = FastAPI(title="Googlers Discord Verification Bot", version="2.0.0")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "model": cfg.gemini_model,
            "bot_token_configured": bool(cfg.discord_bot_token),
            "internal_form_configured": bool(cfg.internal_form_url_template),
        }

    @app.post("/interactions")
    async def discord_interactions(
        request: Request,
        x_signature_ed25519: str | None = Header(default=None),
        x_signature_timestamp: str | None = Header(default=None),
    ) -> JSONResponse:
        raw_body = await request.body()
        if not verify_discord_signature(
            cfg.discord_public_key, x_signature_ed25519, x_signature_timestamp, raw_body
        ):
            raise HTTPException(status_code=401, detail="Invalid request signature")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        result = await handle_interaction(
            payload,
            settings=cfg,
            dispatcher=task_dispatcher,
            discord=discord_client,
            verifier=proof_verifier,
        )
        return JSONResponse(content=result)

    @app.post("/tasks/process-verification")
    async def task_process_verification(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not verify_cloud_tasks_oidc(authorization, cfg):
            raise HTTPException(status_code=403, detail="Unauthorized task invocation")
        payload = await request.json()
        return await process_verification_task(payload, cfg, discord_client, proof_verifier)

    @app.post("/tasks/process-mod-action")
    async def task_process_mod_action(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not verify_cloud_tasks_oidc(authorization, cfg):
            raise HTTPException(status_code=403, detail="Unauthorized task invocation")
        payload = await request.json()
        return await process_mod_action_task(payload, cfg, discord_client)

    @app.post("/internal-verify")
    async def internal_verify(
        body: InternalVerifyRequest,
        authorization: str | None = Header(default=None),
        x_internal_secret: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not cfg.internal_form_secret:
            raise HTTPException(
                status_code=503, detail="Internal verification secret not configured"
            )

        provided = _extract_internal_secret(authorization, x_internal_secret)
        if not provided or not hmac.compare_digest(provided, cfg.internal_form_secret):
            logger.warning("Rejected /internal-verify request due to invalid secret token")
            raise HTTPException(status_code=403, detail="Invalid internal verification token")

        cleaned_user_id = body.user_id.strip()
        if not SNOWFLAKE_RE.match(cleaned_user_id):
            raise HTTPException(
                status_code=400,
                detail="user_id must be a valid numeric Discord user ID (17-20 digits)",
            )

        result = await process_internal_verification(
            user_id=cleaned_user_id,
            notes=body.notes,
            settings=cfg,
            discord=discord_client,
        )
        if result.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=result["message"])
        return result

    return app


app = create_app()
