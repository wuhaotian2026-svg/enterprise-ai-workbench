from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, repr=False)
class NewSessionToken:
    raw_token: str
    token_digest: str

    def __repr__(self) -> str:
        return "NewSessionToken(raw_token='***', token_digest='***')"


def digest_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session_token() -> NewSessionToken:
    raw_token = secrets.token_urlsafe(32)
    return NewSessionToken(
        raw_token=raw_token,
        token_digest=digest_session_token(raw_token),
    )


def session_is_valid(
    *,
    expires_at: datetime,
    revoked_at: datetime | None,
    now: datetime,
) -> bool:
    return revoked_at is None and expires_at > now
