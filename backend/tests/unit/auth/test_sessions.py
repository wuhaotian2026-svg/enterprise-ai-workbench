from __future__ import annotations

from datetime import datetime, timedelta, timezone

from policy_api.auth.sessions import (
    NewSessionToken,
    create_session_token,
    digest_session_token,
    session_is_valid,
)


def test_session_token_is_random_and_only_its_digest_is_persistable() -> None:
    first = create_session_token()
    second = create_session_token()

    assert first.raw_token != second.raw_token
    assert len(first.raw_token) >= 43
    assert len(first.token_digest) == 64
    assert first.token_digest == digest_session_token(first.raw_token)
    assert first.raw_token not in repr(first)


def test_session_is_invalid_when_expired_or_revoked() -> None:
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

    assert session_is_valid(expires_at=now + timedelta(minutes=1), revoked_at=None, now=now)
    assert not session_is_valid(expires_at=now, revoked_at=None, now=now)
    assert not session_is_valid(
        expires_at=now + timedelta(minutes=1),
        revoked_at=now - timedelta(seconds=1),
        now=now,
    )


def test_new_session_token_repr_hides_raw_token_and_digest() -> None:
    token = NewSessionToken(raw_token="raw-secret", token_digest="digest-secret")

    rendered = repr(token)

    assert "raw-secret" not in rendered
    assert "digest-secret" not in rendered
