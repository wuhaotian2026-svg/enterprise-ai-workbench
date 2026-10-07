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
WORKBENCH_TABLES = {
    "organization_units",
    "capability_grants",
    "security_audit_events",
    "product_events",
}


def _alembic_config() -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    return config


def test_third_migration_is_an_explicit_four_table_snapshot() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    source = (
        backend_root / "alembic" / "versions" / "0003_workbench_foundation.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "0003_workbench_foundation"' in source
    assert 'down_revision = "0002_hr_tool_calling"' in source
    assert "from policy_api.models import Base" not in source
    assert "metadata.create_all" not in source
    assert "metadata.drop_all" not in source
    assert '"uq_capability_grants_active_global"' in source
    assert '"uq_capability_grants_active_unit_subtree"' in source
    created_tables = set(
        re.findall(r'op\.create_table\(\s*["\']([^"\']+)["\']', source)
    )
    assert created_tables == WORKBENCH_TABLES


def test_alembic_metadata_registers_workbench_models() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    source = (backend_root / "alembic" / "env.py").read_text(encoding="utf-8")

    assert "policy_api.workbench import audit as _workbench_audit" in source
    assert (
        "policy_api.workbench import capabilities as _workbench_capabilities"
        in source
    )
    assert "policy_api.workbench import events as _workbench_events" in source


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for workbench migration verification",
)
def test_workbench_upgrade_preserves_hr_data_backfills_grants_and_downgrades() -> None:
    assert_test_database_url(TEST_DATABASE_URL)
    config = _alembic_config()
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    engine = create_engine(TEST_DATABASE_URL)
    employee_user_id = uuid.uuid4()
    hr_user_id = uuid.uuid4()
    admin_user_id = uuid.uuid4()
    employee_id = uuid.uuid4()
    leave_type_id = uuid.uuid4()
    account_id = uuid.uuid4()
    request_id = uuid.uuid4()
    invocation_id = uuid.uuid4()
    audit_id = uuid.uuid4()

    try:
        command.downgrade(config, "base")
        command.upgrade(config, "0002_hr_tool_calling")
        with engine.begin() as connection:
            for user_id, username, role in (
                (employee_user_id, "wb-employee", "employee"),
                (hr_user_id, "wb-hr", "hr"),
                (admin_user_id, "wb-admin", "admin"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, username, password_hash, role, is_active, created_at, updated_at) "
                        "VALUES (:id, :username, 'hash', :role, true, now(), now())"
                    ),
                    {"id": user_id, "username": username, "role": role},
                )
            connection.execute(
                text(
                    "INSERT INTO employee_profiles "
                    "(id, user_id, employee_number, display_name, department, hire_date, "
                    "is_active, created_at, updated_at) VALUES "
                    "(:id, :user_id, 'WB-E-1', 'Employee', 'Legacy Department', "
                    "'2024-01-01', true, now(), now())"
                ),
                {"id": employee_id, "user_id": employee_user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO leave_types "
                    "(id, code, display_name, is_enabled, created_at, updated_at) "
                    "VALUES (:id, 'annual', 'Annual', true, now(), now())"
                ),
                {"id": leave_type_id},
            )
            connection.execute(
                text(
                    "INSERT INTO leave_accounts "
                    "(id, employee_id, leave_type_id, year, entitled, used, reserved, "
                    "version, created_at, updated_at) VALUES "
                    "(:id, :employee_id, :leave_type_id, 2026, 10, 0, 1, 1, now(), now())"
                ),
                {
                    "id": account_id,
                    "employee_id": employee_id,
                    "leave_type_id": leave_type_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO leave_requests "
                    "(id, request_number, employee_id, leave_type_id, start_date, end_date, "
                    "workday_count, reason, status, submitted_at, created_at, updated_at) "
                    "VALUES (:id, 'WB-LR-1', :employee_id, :leave_type_id, '2026-08-20', "
                    "'2026-08-20', 1, 'preserved reason', 'pending', now(), now(), now())"
                ),
                {
                    "id": request_id,
                    "employee_id": employee_id,
                    "leave_type_id": leave_type_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO tool_invocations "
                    "(id, conversation_id, turn_id, actor_user_id, provider_call_id, "
                    "tool_name, provider_tool_name, risk_level, status, arguments_hash, "
                    "created_at, updated_at) VALUES "
                    "(:id, :conversation_id, :turn_id, :actor, 'wb-call', "
                    "'hr.get_leave_balance', 'hr_get_leave_balance', 'read', 'succeeded', "
                    ":arguments_hash, now(), now())"
                ),
                {
                    "id": invocation_id,
                    "conversation_id": uuid.uuid4(),
                    "turn_id": uuid.uuid4(),
                    "actor": employee_user_id,
                    "arguments_hash": "a" * 64,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO tool_audit_events "
                    "(id, invocation_id, actor_user_id, event_kind, summary, created_at, "
                    "updated_at) VALUES "
                    "(:id, :invocation_id, :actor, 'execution_succeeded', "
                    "CAST('{}' AS json), now(), now())"
                ),
                {
                    "id": audit_id,
                    "invocation_id": invocation_id,
                    "actor": employee_user_id,
                },
            )

        command.upgrade(config, "head")
        command.upgrade(config, "head")

        assert WORKBENCH_TABLES <= set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM leave_requests WHERE id = :id"),
                {"id": request_id},
            ) == 1
            assert connection.scalar(
                text("SELECT reserved FROM leave_accounts WHERE id = :id"),
                {"id": account_id},
            ) == 1
            assert connection.scalar(
                text("SELECT count(*) FROM tool_audit_events WHERE id = :id"),
                {"id": audit_id},
            ) == 1
            grants = set(
                connection.execute(
                    text(
                        "SELECT user_id, capability, scope_kind FROM capability_grants "
                        "ORDER BY capability"
                    )
                ).all()
            )
            assert grants == {
                (admin_user_id, "knowledge.manage", "global"),
                (admin_user_id, "organization.manage", "global"),
                (admin_user_id, "analytics.view", "global"),
                (hr_user_id, "hr.leave.review", "global"),
            }

        command.downgrade(config, "0002_hr_tool_calling")
        assert not (WORKBENCH_TABLES & set(inspect(engine).get_table_names()))
        employee_columns = {
            column["name"]
            for column in inspect(engine).get_columns("employee_profiles")
        }
        assert "organization_unit_id" not in employee_columns
        assert "manager_employee_id" not in employee_columns
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT department FROM employee_profiles WHERE id = :id"),
                {"id": employee_id},
            ) == "Legacy Department"
            assert connection.scalar(
                text("SELECT count(*) FROM leave_requests WHERE id = :id"),
                {"id": request_id},
            ) == 1
    finally:
        command.downgrade(config, "base")
        engine.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for workbench constraint verification",
)
def test_workbench_constraints_reject_invalid_scope_manager_and_duplicate_audit() -> None:
    assert_test_database_url(TEST_DATABASE_URL)
    config = _alembic_config()
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    engine = create_engine(TEST_DATABASE_URL)
    user_id = uuid.uuid4()
    employee_id = uuid.uuid4()
    root_unit_id = uuid.uuid4()

    def expect_integrity_error(statement: str, parameters: dict) -> None:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(statement), parameters)

    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, password_hash, role, is_active, created_at, updated_at) "
                    "VALUES (:id, 'wb-constraint-user', 'hash', 'employee', true, "
                    "now(), now())"
                ),
                {"id": user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO organization_units "
                    "(id, code, name, is_active, created_at, updated_at) "
                    "VALUES (:id, 'ROOT', 'Root', true, now(), now())"
                ),
                {"id": root_unit_id},
            )
            connection.execute(
                text(
                    "INSERT INTO employee_profiles "
                    "(id, user_id, employee_number, display_name, hire_date, is_active, "
                    "created_at, updated_at) VALUES "
                    "(:id, :user_id, 'WB-C-1', 'Constraint Employee', '2024-01-01', true, "
                    "now(), now())"
                ),
                {"id": employee_id, "user_id": user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO capability_grants "
                    "(id, user_id, capability, scope_kind, organization_unit_id, is_active, "
                    "created_at, updated_at) VALUES "
                    "(:id, :user_id, 'analytics.view', 'global', NULL, true, now(), now())"
                ),
                {"id": uuid.uuid4(), "user_id": user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO capability_grants "
                    "(id, user_id, capability, scope_kind, organization_unit_id, is_active, "
                    "created_at, updated_at) VALUES "
                    "(:id, :user_id, 'hr.leave.review', 'unit_subtree', :unit_id, true, "
                    "now(), now())"
                ),
                {"id": uuid.uuid4(), "user_id": user_id, "unit_id": root_unit_id},
            )
            for _ in range(2):
                connection.execute(
                    text(
                        "INSERT INTO capability_grants "
                        "(id, user_id, capability, scope_kind, organization_unit_id, "
                        "is_active, created_at, updated_at) VALUES "
                        "(:id, :user_id, 'knowledge.manage', 'global', NULL, false, "
                        "now(), now())"
                    ),
                    {"id": uuid.uuid4(), "user_id": user_id},
                )
            connection.execute(
                text(
                    "INSERT INTO security_audit_events "
                    "(id, event_name, actor_user_id, target_type, operation_id, outcome, "
                    "summary, occurred_at, created_at) VALUES "
                    "(:id, 'capability_grant_created', :actor, 'capability_grant', "
                    ":operation, 'succeeded', CAST('{}' AS jsonb), now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "actor": user_id,
                    "operation": uuid.uuid4(),
                },
            )

        expect_integrity_error(
            "UPDATE employee_profiles SET manager_employee_id = id WHERE id = :id",
            {"id": employee_id},
        )
        expect_integrity_error(
            "INSERT INTO capability_grants "
            "(id, user_id, capability, scope_kind, organization_unit_id, is_active, "
            "created_at, updated_at) VALUES "
            "(:id, :user_id, 'organization.manage', 'global', :unit_id, true, now(), now())",
            {"id": uuid.uuid4(), "user_id": user_id, "unit_id": root_unit_id},
        )
        expect_integrity_error(
            "INSERT INTO capability_grants "
            "(id, user_id, capability, scope_kind, organization_unit_id, is_active, "
            "created_at, updated_at) VALUES "
            "(:id, :user_id, 'organization.manage', 'unit_subtree', NULL, true, now(), now())",
            {"id": uuid.uuid4(), "user_id": user_id},
        )
        expect_integrity_error(
            "INSERT INTO capability_grants "
            "(id, user_id, capability, scope_kind, organization_unit_id, is_active, "
            "created_at, updated_at) VALUES "
            "(:id, :user_id, 'analytics.view', 'global', NULL, true, now(), now())",
            {"id": uuid.uuid4(), "user_id": user_id},
        )
        expect_integrity_error(
            "INSERT INTO capability_grants "
            "(id, user_id, capability, scope_kind, organization_unit_id, is_active, "
            "created_at, updated_at) VALUES "
            "(:id, :user_id, 'hr.leave.review', 'unit_subtree', :unit_id, true, now(), now())",
            {"id": uuid.uuid4(), "user_id": user_id, "unit_id": root_unit_id},
        )

        with engine.connect() as connection:
            actor_id, operation_id = connection.execute(
                text(
                    "SELECT actor_user_id, operation_id FROM security_audit_events LIMIT 1"
                )
            ).one()
        expect_integrity_error(
            "INSERT INTO security_audit_events "
            "(id, event_name, actor_user_id, target_type, operation_id, outcome, summary, "
            "occurred_at, created_at) VALUES "
            "(:id, 'capability_grant_revoked', :actor, 'capability_grant', :operation, "
            "'succeeded', CAST('{}' AS jsonb), now(), now())",
            {
                "id": uuid.uuid4(),
                "actor": actor_id,
                "operation": operation_id,
            },
        )
    finally:
        command.downgrade(config, "base")
        engine.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
