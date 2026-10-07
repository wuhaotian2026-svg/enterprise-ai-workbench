from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SlotExtractionRequestIdentity:
    owner_user_id: UUID
    module_key: Literal["hr", "procurement"]
    conversation_id: UUID
    client_turn_id: UUID
    slot_schema_version: str
    slot_schema_sha256: str
    current_user_turn_text: str


@dataclass(frozen=True, slots=True)
class RequestFingerprint:
    value: str
    key_id: str


def canonical_request_bytes(request: SlotExtractionRequestIdentity) -> bytes:
    payload = {
        "client_turn_id": str(request.client_turn_id),
        "conversation_id": str(request.conversation_id),
        "current_user_turn_text": request.current_user_turn_text,
        "module_key": request.module_key,
        "owner_user_id": str(request.owner_user_id),
        "slot_schema_sha256": request.slot_schema_sha256,
        "slot_schema_version": request.slot_schema_version,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class SlotExtractionFingerprinter:
    KEY_ID = "session-secret-hmac-sha256-v1"

    __slots__ = ("_derived_key",)

    def __init__(self, session_secret: bytes) -> None:
        if len(session_secret) < 32:
            raise ValueError("slot_extraction_fingerprint_key_too_short")
        self._derived_key = hmac.digest(
            session_secret,
            b"policy-api:slot-extraction:fingerprint-key:v1",
            "sha256",
        )

    def fingerprint(self, request: SlotExtractionRequestIdentity) -> RequestFingerprint:
        value = hmac.digest(
            self._derived_key,
            b"policy-api:slot-extraction:request:v1\0" + canonical_request_bytes(request),
            "sha256",
        ).hex()
        return RequestFingerprint(value=value, key_id=self.KEY_ID)


def fingerprint_matches(
    expected: RequestFingerprint,
    stored_key_id: str,
    stored_value: str,
) -> bool:
    if stored_key_id != expected.key_id:
        return False
    return hmac.compare_digest(expected.value, stored_value)
