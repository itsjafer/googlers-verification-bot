from __future__ import annotations

import json
import logging
import time
from typing import Any

from google.auth.transport import requests as google_requests
from google.cloud import tasks_v2
from google.oauth2 import id_token
from google.protobuf import timestamp_pb2

from app.config import Settings

logger = logging.getLogger(__name__)


class TaskDispatcher:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: tasks_v2.CloudTasksClient | None = None

    def _get_client(self) -> tasks_v2.CloudTasksClient:
        if self._client is None:
            self._client = tasks_v2.CloudTasksClient()
        return self._client

    def enqueue(self, path: str, payload: dict[str, Any], *, delay_seconds: int = 0) -> str:
        if not self._settings.service_url:
            raise RuntimeError("SERVICE_URL must be configured for Cloud Tasks dispatch")
        if not self._settings.task_service_account:
            raise RuntimeError("TASK_SERVICE_ACCOUNT must be configured for Cloud Tasks OIDC auth")

        client = self._get_client()
        parent = client.queue_path(
            self._settings.gcp_project,
            self._settings.cloud_tasks_location,
            self._settings.cloud_tasks_queue,
        )
        target_url = f"{self._settings.service_url}{path}"
        task: dict[str, Any] = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": target_url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode("utf-8"),
                "oidc_token": {
                    "service_account_email": self._settings.task_service_account,
                    "audience": self._settings.service_url,
                },
            }
        }
        if delay_seconds > 0:
            scheduled = timestamp_pb2.Timestamp()
            scheduled.FromSeconds(int(time.time()) + delay_seconds)
            task["schedule_time"] = scheduled
        created = client.create_task(parent=parent, task=task)
        logger.info("Enqueued Cloud Task %s -> %s (delay=%ds)", created.name, path, delay_seconds)
        return created.name


def verify_cloud_tasks_oidc(authorization_header: str | None, settings: Settings) -> bool:
    if settings.skip_task_auth:
        return True
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return False
    token = authorization_header.removeprefix("Bearer ").strip()
    if not token or not settings.service_url or not settings.task_service_account:
        return False

    try:
        claims = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=settings.service_url,
        )
    except Exception as exc:
        logger.warning("OIDC verification failed: %s", exc)
        return False

    email = claims.get("email")
    email_verified = claims.get("email_verified", False)
    if not email_verified or email != settings.task_service_account:
        logger.warning("OIDC token email mismatch: got %s", email)
        return False
    return True
