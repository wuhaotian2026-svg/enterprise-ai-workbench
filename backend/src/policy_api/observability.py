from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4


logger = logging.getLogger("policy_api")
ALLOWED_FIELDS = {"request_id", "resource_id", "stage", "duration_ms", "error_code",
                  "method", "path", "status_code", "actor_id", "tool_name",
                  "risk_level", "operation_id", "outcome"}


class InvalidRequestId(ValueError):
    """Raised when a caller-supplied request identifier is unsafe."""


def normalize_request_id(value: str | None) -> str:
    request_id = value or str(uuid4())
    if len(request_id) > 120 or any(
        ord(character) < 32 or ord(character) == 127
        for character in request_id
    ):
        raise InvalidRequestId("request_id_invalid")
    return request_id


def log_event(event: str, **fields: Any) -> None:
    # Alembic's fileConfig may disable pre-existing loggers in a long-lived process.
    logger.disabled = False
    payload = {"event": event}
    payload.update({key: value for key, value in fields.items() if key in ALLOWED_FIELDS and value is not None})
    logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
