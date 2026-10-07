from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DatabaseSession

from policy_api.auth.passwords import verify_password
from policy_api.auth.sessions import NewSessionToken, create_session_token, digest_session_token, session_is_valid
from policy_api.models import Session, User, utc_now


SESSION_LIFETIME = timedelta(hours=8)


def authenticate(database: DatabaseSession, username: str, password: str) -> User | None:
    user = database.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def issue_session(database: DatabaseSession, user: User) -> NewSessionToken:
    token = create_session_token()
    database.add(
        Session(
            user_id=user.id,
            token_digest=token.token_digest,
            expires_at=utc_now() + SESSION_LIFETIME,
        )
    )
    database.commit()
    return token


def resolve_user(database: DatabaseSession, raw_token: str | None) -> tuple[User, Session] | None:
    if not raw_token:
        return None
    row = database.execute(
        select(User, Session)
        .join(Session, Session.user_id == User.id)
        .where(Session.token_digest == digest_session_token(raw_token), User.is_active.is_(True))
    ).one_or_none()
    if row is None or not session_is_valid(expires_at=row.Session.expires_at, revoked_at=row.Session.revoked_at, now=utc_now()):
        return None
    return row.User, row.Session


def revoke_session(database: DatabaseSession, session: Session) -> None:
    session.revoked_at = utc_now()
    database.commit()
