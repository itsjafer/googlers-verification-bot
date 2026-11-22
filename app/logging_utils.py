from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any


class CloudRunJSONFormatter(logging.Formatter):
    """Formats logs as structured JSON compatible with Google Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key in ("user_id", "target_user_id", "verdict", "confidence", "action", "status"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    root = logging.getLogger()
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level, logging.INFO))

    handler = logging.StreamHandler()
    if os.environ.get("K_SERVICE"):
        handler.setFormatter(CloudRunJSONFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    root.handlers.clear()
    root.addHandler(handler)
