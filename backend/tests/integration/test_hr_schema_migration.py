from __future__ import annotations

import os
from pathlib import Path
import re
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from policy_api.database import assert_test_database_url


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
HR_AND_TOOL_TABLES = {
    "employee_profiles",
    "leave_types",
    "leave_accounts",
    "leave_account_events",
    "work_calendar_days",
    "leave_requests",
    "hr_conversations",
    "hr_turns",
    "tool_invocations",
    "tool_confirmations",
    "tool_audit_events",
}


def test_second_migration_is_an_explicit_eleven_table_snapshot() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    source = (
        backend_root / "alembic" / "versions" / "0002_hr_tool_calling.py"
    ).read_text(encoding="utf-8")

    assert "from policy_api.models import Base" not in source
    assert "metadata.create_all" not in source
    assert "metadata.drop_all" not in source
    created_tables = set(
        re.findall(r'op\.create_table\(\s*["\']([^"\']+)["\']', source)
    )
    assert created_tables == HR_AND_TOOL_TABLES


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for HR schema migration verification",
)
def test_head_contains_explicit_hr_conversation_and_tool_tables() -> None:
    assert_test_database_url(TEST_DATABASE_URL)
    backend_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(backend_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_root / "alembic"))
    original_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    engine = create_engine(TEST_DATABASE_URL)
    try:
        command.downgrade(alembic_config, "base")
        command.upgrade(alembic_config, "head")
        assert HR_AND_TOOL_TABLES <= set(inspect(engine).get_table_names())
    finally:
        command.downgrade(alembic_config, "base")
        engine.dispose()
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for HR constraint verification",
)
def test_hr_balance_and_request_constraints_reject_invalid_rows() -> None:
    assert_test_database_url(TEST_DATABASE_URL)
    backend_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(backend_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_root / "alembic"))
    original_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    engine = create_engine(TEST_DATABASE_URL)
    user_id = uuid.uuid4()
    employee_id = uuid.uuid4()
    leave_type_id = uuid.uuid4()
    try:
        command.downgrade(alembic_config, "base")
        command.upgrade(alembic_config, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, password_hash, role, is_active, created_at, updated_at) "
                    "VALUES (:id, :username, 'hash', 'employee', true, now(), now())"
                ),
                {"id": user_id, "username": f"employee-{user_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO employee_profiles "
                    "(id, user_id, employee_number, display_name, hire_date, is_active, "
                    "created_at, updated_at) "
                    "VALUES (:id, :user_id, :number, 'Employee', '2024-01-01', true, "
                    "now(), now())"
                ),
                {"id": employee_id, "user_id": user_id, "number": f"E-{user_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO leave_types "
                    "(id, code, display_name, is_enabled, created_at, updated_at) "
                    "VALUES (:id, 'annual', 'Annual', true, now(), now())"
                ),
                {"id": leave_type_id},
            )

        invalid_accounts = [
            {"entitled": 10, "used": -1, "reserved": 0},
            {"entitled": 10, "used": 7, "reserved": 4},
        ]
        for values in invalid_accounts:
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO leave_accounts "
                            "(id, employee_id, leave_type_id, year, entitled, used, reserved, "
                            "version, created_at, updated_at) "
                            "VALUES (:id, :employee_id, :leave_type_id, 2026, :entitled, "
                            ":used, :reserved, 1, now(), now())"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "employee_id": employee_id,
                            "leave_type_id": leave_type_id,
                            **values,
                        },
                    )

        invalid_requests = [
            {"number": "LR-DATES", "start": "2026-08-20", "end": "2026-08-19", "days": 1},
            {"number": "LR-DAYS", "start": "2026-08-20", "end": "2026-08-20", "days": 0},
        ]
        for values in invalid_requests:
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO leave_requests "
                            "(id, request_number, employee_id, leave_type_id, start_date, "
                            "end_date, workday_count, reason, status, submitted_at, "
                            "created_at, updated_at) "
                            "VALUES (:id, :number, :employee_id, :leave_type_id, :start, "
                            ":end, :days, 'test', 'pending', now(), now(), now())"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "employee_id": employee_id,
                            "leave_type_id": leave_type_id,
                            **values,
                        },
                    )
    finally:
        command.downgrade(alembic_config, "base")
        engine.dispose()
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url
