from __future__ import annotations

import os

from sqlalchemy import select

from policy_api.auth.passwords import hash_password, verify_password
from policy_api.config import Settings
from policy_api.database import create_database_engine, create_session_factory
from policy_api.models import User, UserRole


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def ensure_user(database, username: str, password: str, role: UserRole) -> str:  # type: ignore[no-untyped-def]
    user = database.scalar(select(User).where(User.username == username))
    if user is None:
        database.add(User(username=username, password_hash=hash_password(password), role=role))
        return "created"
    changed = False
    if user.role != role:
        user.role = role
        changed = True
    if not verify_password(password, user.password_hash):
        user.password_hash = hash_password(password)
        changed = True
    return "updated" if changed else "unchanged"


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    engine = create_database_engine(settings.database_url)
    sessions = create_session_factory(engine)
    with sessions.begin() as database:
        admin = ensure_user(database, os.getenv("DEMO_ADMIN_USERNAME", "admin"), required("DEMO_ADMIN_PASSWORD"), UserRole.ADMIN)
        employee = ensure_user(database, os.getenv("DEMO_EMPLOYEE_USERNAME", "employee"), required("DEMO_EMPLOYEE_PASSWORD"), UserRole.EMPLOYEE)
    engine.dispose()
    print(f"demo users: admin={admin}, employee={employee}")


if __name__ == "__main__":
    main()
