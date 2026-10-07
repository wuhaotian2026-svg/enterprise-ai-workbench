from __future__ import annotations

import os
from pathlib import Path
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from policy_api.database import assert_test_database_url
from policy_api.models import User, UserRole


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required",
)
def test_user_role_storage_reads_names_and_values_but_writes_canonical_values(
) -> None:
    assert TEST_DATABASE_URL is not None
    assert_test_database_url(TEST_DATABASE_URL)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    original_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    try:
        command.upgrade(config, "head")
    finally:
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url

    engine = create_engine(TEST_DATABASE_URL)
    lower_id = uuid.uuid4()
    upper_id = uuid.uuid4()
    canonical_id = uuid.uuid4()
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, username, password_hash, role, is_active, created_at, updated_at) "
                "VALUES (:lower_id, :lower_username, 'hash', 'employee', true, now(), now()), "
                "(:upper_id, :upper_username, 'hash', 'HR', true, now(), now())"
            ),
            {
                "lower_id": lower_id,
                "lower_username": f"role-lower-{lower_id}",
                "upper_id": upper_id,
                "upper_username": f"role-upper-{upper_id}",
            },
        )
        with Session(bind=connection) as db:
            users = {
                user.id: user
                for user in db.scalars(
                    select(User).where(User.id.in_((lower_id, upper_id)))
                )
            }
            assert users[lower_id].role is UserRole.EMPLOYEE
            assert users[upper_id].role is UserRole.HR

            db.add(
                User(
                    id=canonical_id,
                    username=f"role-canonical-{canonical_id}",
                    password_hash="hash",
                    role=UserRole.ADMIN,
                    is_active=True,
                )
            )
            db.flush()
            stored_role = connection.scalar(
                text("SELECT role FROM users WHERE id = :id"),
                {"id": canonical_id},
            )
            assert stored_role == "admin"
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
