from __future__ import annotations

import os
from pathlib import Path
import re
import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from policy_api.approvals import models as _approval_models  # noqa: F401
from policy_api.database import assert_test_database_url
from policy_api.models import Base
from policy_api.procurement import models as _procurement_models  # noqa: F401

PROCUREMENT_APPROVAL_TABLES = {
    "approval_instances",
    "approval_tasks",
    "approval_decisions",
    "procurement_requests",
    "procurement_request_items",
    "procurement_command_operations",
    "assistant_conversations",
    "assistant_turns",
    "approval_command_operations",
}
FIFTH_REVISION_TABLES = PROCUREMENT_APPROVAL_TABLES - {"approval_command_operations"}


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL", "")
    if not value:
        pytest.fail("TEST_DATABASE_URL is required for procurement migration verification")
    assert_test_database_url(value)
    return value


def _alembic_config() -> Config:
    backend_root = _backend_root()
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    return config


@pytest.fixture()
def migrated_database():
    database_url = _database_url()
    previous_url = os.environ.get("DATABASE_URL")
    config = _alembic_config()
    engine = None
    try:
        os.environ["DATABASE_URL"] = database_url
        engine = create_engine(database_url)
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        yield engine
    finally:
        try:
            if engine is not None:
                command.downgrade(config, "base")
                command.upgrade(config, "head")
        finally:
            if engine is not None:
                engine.dispose()
            if previous_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_url


def _seed_principals(engine) -> dict[str, uuid.UUID]:
    values = {
        "applicant_user_id": uuid.uuid4(),
        "reviewer_user_id": uuid.uuid4(),
        "employee_id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
    }
    with engine.begin() as connection:
        for key, username in (
            ("applicant_user_id", "procurement-applicant"),
            ("reviewer_user_id", "procurement-reviewer"),
        ):
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, password_hash, role, is_active, created_at, updated_at) "
                    "VALUES (:id, :username, 'hash', 'employee', true, now(), now())"
                ),
                {"id": values[key], "username": username},
            )
        connection.execute(
            text(
                "INSERT INTO organization_units "
                "(id, code, name, is_active, created_at, updated_at) "
                "VALUES (:id, 'PROC-UNIT', 'Procurement Unit', true, now(), now())"
            ),
            {"id": values["organization_id"]},
        )
        connection.execute(
            text(
                "INSERT INTO employee_profiles "
                "(id, user_id, employee_number, display_name, hire_date, is_active, "
                "organization_unit_id, created_at, updated_at) VALUES "
                "(:id, :user_id, 'PROC-E-1', 'Applicant', '2024-01-01', true, "
                ":organization_id, now(), now())"
            ),
            {
                "id": values["employee_id"],
                "user_id": values["applicant_user_id"],
                "organization_id": values["organization_id"],
            },
        )
    return values


def _insert_instance(connection, values: dict[str, uuid.UUID]) -> uuid.UUID:
    instance_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO approval_instances "
            "(id, process_key, process_version, subject_type, applicant_user_id, "
            "organization_unit_id, status, current_step_key, version, submitted_at, "
            "created_at, updated_at) VALUES "
            "(:id, 'procurement_request', 1, 'procurement_request', :applicant_user_id, "
            ":organization_id, 'running', 'manager_review', 1, now(), now(), now())"
        ),
        {
            "id": instance_id,
            "applicant_user_id": values["applicant_user_id"],
            "organization_id": values["organization_id"],
        },
    )
    return instance_id


def _insert_task(
    connection,
    values: dict[str, uuid.UUID],
    instance_id: uuid.UUID,
    *,
    sequence: int,
    status: str = "pending",
) -> uuid.UUID:
    task_id = uuid.uuid4()
    activated_at_sql = "now()" if status == "pending" else "NULL"
    connection.execute(
        text(
            "INSERT INTO approval_tasks "
            "(id, instance_id, sequence, step_key, step_label, assignment_kind, "
            "assigned_user_id, status, activated_at, created_at, updated_at) VALUES "
            "(:id, :instance_id, :sequence, :step_key, 'Review', 'user', "
            ":reviewer_user_id, :status, "
            f"{activated_at_sql}, now(), now())"
        ),
        {
            "id": task_id,
            "instance_id": instance_id,
            "sequence": sequence,
            "step_key": f"step-{sequence}",
            "reviewer_user_id": values["reviewer_user_id"],
            "status": status,
        },
    )
    return task_id


def _insert_request(
    connection,
    values: dict[str, uuid.UUID],
    instance_id: uuid.UUID,
    *,
    total_amount: str = "100.00",
    currency: str = "CNY",
) -> uuid.UUID:
    request_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO procurement_requests "
            "(id, request_number, approval_instance_id, applicant_employee_id, "
            "organization_unit_id, title, purpose, needed_by_date, currency, total_amount, "
            "submitted_at, created_at, updated_at) VALUES "
            "(:id, :request_number, :instance_id, :employee_id, :organization_id, "
            "'Office supplies', 'Operational need', '2026-09-01', :currency, "
            ":total_amount, now(), now(), now())"
        ),
        {
            "id": request_id,
            "request_number": f"PR-{request_id}",
            "instance_id": instance_id,
            "employee_id": values["employee_id"],
            "organization_id": values["organization_id"],
            "currency": currency,
            "total_amount": total_amount,
        },
    )
    return request_id


def _seed_historical_records(engine) -> dict[str, uuid.UUID]:
    ids = {
        "user": uuid.uuid4(),
        "document": uuid.uuid4(),
        "leave_type": uuid.uuid4(),
        "tool_invocation": uuid.uuid4(),
        "organization_unit": uuid.uuid4(),
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, username, password_hash, role, is_active, created_at, updated_at) "
                "VALUES (:id, 'pre-procurement', 'hash', 'employee', true, now(), now())"
            ),
            {"id": ids["user"]},
        )
        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, display_name, storage_key, sha256, mime_type, status, is_enabled, "
                "created_at, updated_at) VALUES "
                "(:id, 'Historical Policy', 'history/policy.pdf', :sha256, "
                "'application/pdf', 'enabled', true, now(), now())"
            ),
            {"id": ids["document"], "sha256": "c" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO leave_types "
                "(id, code, display_name, is_enabled, created_at, updated_at) "
                "VALUES (:id, 'history', 'Historical Leave', true, now(), now())"
            ),
            {"id": ids["leave_type"]},
        )
        connection.execute(
            text(
                "INSERT INTO tool_invocations "
                "(id, conversation_id, turn_id, actor_user_id, provider_call_id, "
                "tool_name, provider_tool_name, risk_level, status, arguments_hash, "
                "duration_ms, created_at, updated_at) VALUES "
                "(:id, :conversation_id, :turn_id, :actor_user_id, 'history-call', "
                "'knowledge.search_policy', 'knowledge_search_policy', 'read', "
                "'succeeded', :arguments_hash, 12, now(), now())"
            ),
            {
                "id": ids["tool_invocation"],
                "conversation_id": uuid.uuid4(),
                "turn_id": uuid.uuid4(),
                "actor_user_id": ids["user"],
                "arguments_hash": "d" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO organization_units "
                "(id, code, name, is_active, created_at, updated_at) "
                "VALUES (:id, 'HISTORY', 'Historical Unit', true, now(), now())"
            ),
            {"id": ids["organization_unit"]},
        )
    return ids


def _assert_historical_records(engine, ids: dict[str, uuid.UUID]) -> None:
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT username, role, is_active FROM users WHERE id = :id"),
            {"id": ids["user"]},
        ).one() == ("pre-procurement", "employee", True)
        assert connection.execute(
            text(
                "SELECT display_name, storage_key, is_enabled "
                "FROM documents WHERE id = :id"
            ),
            {"id": ids["document"]},
        ).one() == ("Historical Policy", "history/policy.pdf", True)
        assert connection.execute(
            text("SELECT code, display_name, is_enabled FROM leave_types WHERE id = :id"),
            {"id": ids["leave_type"]},
        ).one() == ("history", "Historical Leave", True)
        assert connection.execute(
            text(
                "SELECT tool_name, status, duration_ms "
                "FROM tool_invocations WHERE id = :id"
            ),
            {"id": ids["tool_invocation"]},
        ).one() == ("knowledge.search_policy", "succeeded", 12)
        assert connection.execute(
            text("SELECT code, name, is_active FROM organization_units WHERE id = :id"),
            {"id": ids["organization_unit"]},
        ).one() == ("HISTORY", "Historical Unit", True)


def test_fifth_migration_is_an_explicit_eight_table_snapshot() -> None:
    source = (
        _backend_root()
        / "alembic"
        / "versions"
        / "0005_procurement_approval_center.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "0005_procurement_approval_center"' in source
    assert 'down_revision = "0004_rag_clarification"' in source
    assert "from policy_api.models import Base" not in source
    assert "metadata.create_all" not in source
    assert "metadata.drop_all" not in source
    created_tables = set(
        re.findall(r'op\.create_table\(\s*["\']([^"\']+)["\']', source)
    )
    assert created_tables == FIFTH_REVISION_TABLES


def test_sixth_migration_explicitly_adds_only_approval_command_operations() -> None:
    migration_path = (
        _backend_root()
        / "alembic"
        / "versions"
        / "0006_approval_command_operations.py"
    )
    assert migration_path.exists()
    source = migration_path.read_text(encoding="utf-8")

    assert 'revision = "0006_approval_command_operations"' in source
    assert 'down_revision = "0005_procurement_approval_center"' in source
    assert "from policy_api.models import Base" not in source
    assert "metadata.create_all" not in source
    assert "metadata.drop_all" not in source
    assert set(
        re.findall(r'op\.create_table\(\s*["\']([^"\']+)["\']', source)
    ) == {"approval_command_operations"}
    assert 'op.drop_table("approval_command_operations")' in source


def test_alembic_metadata_registers_approval_and_procurement_models() -> None:
    source = (_backend_root() / "alembic" / "env.py").read_text(encoding="utf-8")

    assert "policy_api.approvals import models as _approval_models" in source
    assert "policy_api.procurement import models as _procurement_models" in source


def test_head_has_frozen_tables_columns_constraints_and_queue_indexes(
    migrated_database,
) -> None:
    inspector = inspect(migrated_database)
    assert PROCUREMENT_APPROVAL_TABLES <= set(inspector.get_table_names())
    assert "assistant_flow_drafts" in inspector.get_table_names()
    assert {
        column["name"]
        for column in inspector.get_columns("assistant_turns")
    } >= {"request_content", "content"}
    request_content = next(
        column
        for column in inspector.get_columns("assistant_turns")
        if column["name"] == "request_content"
    )
    assert request_content["nullable"] is False
    assert {
        column["name"]
        for column in inspector.get_columns("assistant_flow_drafts")
    } == {
        "id",
        "owner_user_id",
        "module_key",
        "conversation_id",
        "intent",
        "status",
        "version",
        "field_values",
        "field_sources",
        "pending_candidates",
        "expires_at",
        "created_at",
        "updated_at",
    }
    assert {
        "uq_approval_tasks_one_pending_per_instance",
        "ix_approval_tasks_user_queue",
        "ix_approval_tasks_capability_scope_queue",
    } <= {index["name"] for index in inspector.get_indexes("approval_tasks")}
    assert {column["name"] for column in inspector.get_columns("approval_decisions")} == {
        "id",
        "instance_id",
        "task_id",
        "actor_user_id",
        "action",
        "comment",
        "client_operation_id",
        "decided_at",
        "created_at",
    }
    assert {
        column["name"]
        for column in inspector.get_columns("approval_command_operations")
    } == {
        "id",
        "actor_user_id",
        "client_operation_id",
        "command_kind",
        "instance_id",
        "task_id",
        "canonical_payload_hash",
        "status",
        "completed_at",
        "created_at",
        "updated_at",
    }


def test_approval_command_constraints_reject_invalid_ledger_rows(
    migrated_database,
) -> None:
    engine = migrated_database
    values = _seed_principals(engine)
    with engine.begin() as connection:
        first_instance_id = _insert_instance(connection, values)
        first_task_id = _insert_task(connection, values, first_instance_id, sequence=1)
        second_instance_id = _insert_instance(connection, values)
        second_task_id = _insert_task(connection, values, second_instance_id, sequence=1)

    def insert_operation(
        *,
        command_kind: str,
        instance_id: uuid.UUID,
        task_id: uuid.UUID | None,
        payload_hash: str = "e" * 64,
        status: str = "in_progress",
        completed: bool = False,
        actor_user_id: uuid.UUID = values["reviewer_user_id"],
        client_operation_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        operation_id = client_operation_id or uuid.uuid4()
        completed_sql = "now()" if completed else "NULL"
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO approval_command_operations "
                    "(id, actor_user_id, client_operation_id, command_kind, instance_id, "
                    "task_id, canonical_payload_hash, status, completed_at, created_at, "
                    "updated_at) VALUES "
                    "(:id, :actor_user_id, :client_operation_id, :command_kind, "
                    ":instance_id, :task_id, :payload_hash, :status, "
                    f"{completed_sql}, now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "actor_user_id": actor_user_id,
                    "client_operation_id": operation_id,
                    "command_kind": command_kind,
                    "instance_id": instance_id,
                    "task_id": task_id,
                    "payload_hash": payload_hash,
                    "status": status,
                },
            )
        return operation_id

    insert_operation(
        command_kind="approval.approve",
        instance_id=first_instance_id,
        task_id=first_task_id,
    )
    insert_operation(
        command_kind="approval.cancel",
        instance_id=first_instance_id,
        task_id=None,
        status="succeeded",
        completed=True,
    )

    invalid_rows = (
        {
            "command_kind": "approval.return",
            "instance_id": first_instance_id,
            "task_id": first_task_id,
        },
        {
            "command_kind": "approval.approve",
            "instance_id": first_instance_id,
            "task_id": None,
        },
        {
            "command_kind": "approval.reject",
            "instance_id": first_instance_id,
            "task_id": None,
        },
        {
            "command_kind": "approval.cancel",
            "instance_id": first_instance_id,
            "task_id": first_task_id,
        },
        {
            "command_kind": "approval.approve",
            "instance_id": first_instance_id,
            "task_id": first_task_id,
            "payload_hash": "short",
        },
        {
            "command_kind": "approval.approve",
            "instance_id": first_instance_id,
            "task_id": first_task_id,
            "status": "failed",
        },
        {
            "command_kind": "approval.approve",
            "instance_id": first_instance_id,
            "task_id": first_task_id,
            "status": "in_progress",
            "completed": True,
        },
        {
            "command_kind": "approval.approve",
            "instance_id": first_instance_id,
            "task_id": first_task_id,
            "status": "succeeded",
            "completed": False,
        },
        {
            "command_kind": "approval.approve",
            "instance_id": first_instance_id,
            "task_id": second_task_id,
        },
    )
    for row in invalid_rows:
        with pytest.raises(IntegrityError):
            insert_operation(**row)

    duplicate_operation_id = uuid.uuid4()
    insert_operation(
        command_kind="approval.approve",
        instance_id=second_instance_id,
        task_id=second_task_id,
        client_operation_id=duplicate_operation_id,
    )
    with pytest.raises(IntegrityError):
        insert_operation(
            command_kind="approval.cancel",
            instance_id=second_instance_id,
            task_id=None,
            client_operation_id=duplicate_operation_id,
        )


def test_autogenerate_has_no_approval_or_procurement_schema_drift(
    migrated_database,
) -> None:
    with migrated_database.connect() as connection:
        differences = compare_metadata(
            MigrationContext.configure(connection),
            Base.metadata,
        )
    task_two_differences = [
        difference
        for difference in differences
        if any(table_name in repr(difference) for table_name in PROCUREMENT_APPROVAL_TABLES)
    ]
    assert task_two_differences == []


def test_approval_constraints_reject_invalid_tasks_and_decisions(
    migrated_database,
) -> None:
    engine = migrated_database
    values = _seed_principals(engine)
    with engine.begin() as connection:
        first_instance_id = _insert_instance(connection, values)
        first_task_id = _insert_task(connection, values, first_instance_id, sequence=1)
        second_instance_id = _insert_instance(connection, values)
        second_task_id = _insert_task(connection, values, second_instance_id, sequence=1)

    for status, current_step_key, completed_at_sql, version in (
        ("running", None, "NULL", 1),
        ("approved", "still-active", "now()", 1),
        ("running", "manager-review", "NULL", 0),
    ):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO approval_instances "
                        "(id, process_key, process_version, subject_type, applicant_user_id, "
                        "organization_unit_id, status, current_step_key, version, "
                        "submitted_at, completed_at, created_at, updated_at) VALUES "
                        "(:id, 'procurement_request', 1, 'procurement_request', "
                        ":applicant_user_id, :organization_id, :status, :current_step_key, "
                        f":version, now(), {completed_at_sql}, now(), now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "applicant_user_id": values["applicant_user_id"],
                        "organization_id": values["organization_id"],
                        "status": status,
                        "current_step_key": current_step_key,
                        "version": version,
                    },
                )

    invalid_task_statements = (
        (
            "INSERT INTO approval_tasks "
            "(id, instance_id, sequence, step_key, step_label, assignment_kind, status, "
            "activated_at, created_at, updated_at) VALUES "
            "(:id, :instance_id, 2, 'missing-user', 'Invalid', 'user', 'waiting', "
            "NULL, now(), now())",
            {"id": uuid.uuid4(), "instance_id": first_instance_id},
        ),
        (
            "INSERT INTO approval_tasks "
            "(id, instance_id, sequence, step_key, step_label, assignment_kind, "
            "assigned_user_id, status, created_at, updated_at) VALUES "
            "(:id, :instance_id, 3, 'invalid-pending', 'Invalid', 'user', "
            ":reviewer_user_id, 'pending', now(), now())",
            {
                "id": uuid.uuid4(),
                "instance_id": first_instance_id,
                "reviewer_user_id": values["reviewer_user_id"],
            },
        ),
        (
            "INSERT INTO approval_tasks "
            "(id, instance_id, sequence, step_key, step_label, assignment_kind, "
            "assigned_user_id, status, activated_at, created_at, updated_at) VALUES "
            "(:id, :instance_id, 4, 'second-pending', 'Invalid', 'user', "
            ":reviewer_user_id, 'pending', now(), now(), now())",
            {
                "id": uuid.uuid4(),
                "instance_id": first_instance_id,
                "reviewer_user_id": values["reviewer_user_id"],
            },
        ),
    )
    for statement, parameters in invalid_task_statements:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(statement), parameters)

    decision_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO approval_decisions "
                "(id, instance_id, task_id, actor_user_id, action, client_operation_id, "
                "decided_at, created_at) VALUES "
                "(:id, :instance_id, :task_id, :actor_user_id, 'approve', :operation_id, "
                "now(), now())"
            ),
            {
                "id": decision_id,
                "instance_id": first_instance_id,
                "task_id": first_task_id,
                "actor_user_id": values["reviewer_user_id"],
                "operation_id": operation_id,
            },
        )

    invalid_decisions = [
        {
            "instance_id": first_instance_id,
            "task_id": first_task_id,
            "operation_id": uuid.uuid4(),
            "action": "approve",
            "comment": None,
        },
        {
            "instance_id": first_instance_id,
            "task_id": second_task_id,
            "operation_id": uuid.uuid4(),
            "action": "approve",
            "comment": None,
        },
        {
            "instance_id": second_instance_id,
            "task_id": second_task_id,
            "operation_id": operation_id,
            "action": "approve",
            "comment": None,
        },
    ]
    invalid_decisions.extend(
        {
            "instance_id": second_instance_id,
            "task_id": second_task_id,
            "operation_id": uuid.uuid4(),
            "action": "reject",
            "comment": comment,
        }
        for comment in ("", "   ", "\t", "\n")
    )
    for invalid in invalid_decisions:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO approval_decisions "
                        "(id, instance_id, task_id, actor_user_id, action, comment, "
                        "client_operation_id, decided_at, created_at) VALUES "
                        "(:id, :instance_id, :task_id, :actor_user_id, :action, :comment, "
                        ":operation_id, now(), now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "actor_user_id": values["reviewer_user_id"],
                        **invalid,
                    },
                )


def test_procurement_constraints_reject_invalid_totals_items_and_categories(
    migrated_database,
) -> None:
    engine = migrated_database
    values = _seed_principals(engine)
    with engine.begin() as connection:
        boundary_instance_id = _insert_instance(connection, values)
        boundary_request_id = _insert_request(
            connection,
            values,
            boundary_instance_id,
            total_amount="999999999999.99",
        )
        connection.execute(
            text(
                "INSERT INTO procurement_request_items "
                "(id, request_id, line_number, category_code, item_name, quantity, unit, "
                "estimated_unit_price, subtotal, created_at, updated_at) VALUES "
                "(:id, :request_id, 1, 'office_supplies', 'Paper', 1, 'pack', 10, 10, "
                "now(), now())"
            ),
            {"id": uuid.uuid4(), "request_id": boundary_request_id},
        )

    for total_amount, currency in (
        ("1000000000000.00", "CNY"),
        ("-0.01", "CNY"),
        ("100.00", "USD"),
    ):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                instance_id = _insert_instance(connection, values)
                _insert_request(
                    connection,
                    values,
                    instance_id,
                    total_amount=total_amount,
                    currency=currency,
                )

    invalid_items = (
        {"line": 0, "category": "other", "quantity": 1, "price": 1, "subtotal": 1},
        {"line": 2, "category": "unapproved", "quantity": 1, "price": 1, "subtotal": 1},
        {"line": 3, "category": "other", "quantity": 0, "price": 1, "subtotal": 1},
        {"line": 4, "category": "other", "quantity": 1, "price": -1, "subtotal": 1},
        {"line": 5, "category": "other", "quantity": 1, "price": 1, "subtotal": -1},
        {"line": 1, "category": "other", "quantity": 1, "price": 1, "subtotal": 1},
    )
    for item in invalid_items:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO procurement_request_items "
                        "(id, request_id, line_number, category_code, item_name, quantity, "
                        "unit, estimated_unit_price, subtotal, created_at, updated_at) "
                        "VALUES (:id, :request_id, :line, :category, 'Invalid', :quantity, "
                        "'unit', :price, :subtotal, now(), now())"
                    ),
                    {"id": uuid.uuid4(), "request_id": boundary_request_id, **item},
                )

    operation_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO procurement_command_operations "
                "(id, actor_user_id, client_operation_id, command_kind, "
                "canonical_payload_hash, status, created_at, updated_at) VALUES "
                "(:id, :actor_user_id, :operation_id, 'submit', :payload_hash, "
                "'in_progress', now(), now())"
            ),
            {
                "id": uuid.uuid4(),
                "actor_user_id": values["applicant_user_id"],
                "operation_id": operation_id,
                "payload_hash": "a" * 64,
            },
        )
    invalid_operations = (
        {
            "operation_id": operation_id,
            "status": "in_progress",
            "resource_type": None,
            "resource_id": None,
        },
        {
            "operation_id": uuid.uuid4(),
            "status": "in_progress",
            "resource_type": "procurement_request",
            "resource_id": uuid.uuid4(),
        },
        {
            "operation_id": uuid.uuid4(),
            "status": "succeeded",
            "resource_type": None,
            "resource_id": None,
        },
    )
    for invalid in invalid_operations:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO procurement_command_operations "
                        "(id, actor_user_id, client_operation_id, command_kind, "
                        "canonical_payload_hash, status, result_resource_type, "
                        "result_resource_id, created_at, updated_at) VALUES "
                        "(:id, :actor_user_id, :operation_id, 'submit', :payload_hash, "
                        ":status, :resource_type, :resource_id, now(), now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "actor_user_id": values["applicant_user_id"],
                        "payload_hash": "b" * 64,
                        **invalid,
                    },
                )


def test_assistant_turn_owner_and_module_must_match_conversation(
    migrated_database,
) -> None:
    engine = migrated_database
    values = _seed_principals(engine)
    conversation_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO assistant_conversations "
                "(id, owner_user_id, module_key, title, is_archived, created_at, updated_at) "
                "VALUES (:id, :owner_user_id, 'procurement', 'Conversation', false, "
                "now(), now())"
            ),
            {"id": conversation_id, "owner_user_id": values["applicant_user_id"]},
        )
        client_turn_id = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO assistant_turns "
                "(id, conversation_id, owner_user_id, module_key, client_turn_id, role, "
                "request_content, content, blocks, created_at, updated_at) VALUES "
                "(:id, :conversation_id, :owner_user_id, 'procurement', :client_turn_id, "
                "'user', 'hello', 'hello', '[]', now(), now())"
            ),
            {
                "id": uuid.uuid4(),
                "conversation_id": conversation_id,
                "owner_user_id": values["applicant_user_id"],
                "client_turn_id": client_turn_id,
            },
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO assistant_turns "
                    "(id, conversation_id, owner_user_id, module_key, client_turn_id, role, "
                    "request_content, content, blocks, created_at, updated_at) VALUES "
                    "(:id, :conversation_id, :owner_user_id, 'hr', :client_turn_id, 'user', "
                    "'hello', 'hello', '[]', now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "conversation_id": conversation_id,
                    "owner_user_id": values["applicant_user_id"],
                    "client_turn_id": uuid.uuid4(),
                },
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO assistant_turns "
                    "(id, conversation_id, owner_user_id, module_key, client_turn_id, role, "
                    "request_content, content, blocks, created_at, updated_at) VALUES "
                    "(:id, :conversation_id, :owner_user_id, 'procurement', "
                    ":client_turn_id, 'user', 'wrong owner', 'wrong owner', '[]', now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "conversation_id": conversation_id,
                    "owner_user_id": values["reviewer_user_id"],
                    "client_turn_id": uuid.uuid4(),
                },
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO assistant_turns "
                    "(id, conversation_id, owner_user_id, module_key, client_turn_id, role, "
                    "request_content, content, blocks, created_at, updated_at) VALUES "
                    "(:id, :conversation_id, :owner_user_id, 'procurement', :client_turn_id, "
                    "'user', 'duplicate', 'duplicate', '[]', now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "conversation_id": conversation_id,
                    "owner_user_id": values["applicant_user_id"],
                    "client_turn_id": client_turn_id,
                },
            )


def test_round_trip_from_fourth_revision_preserves_historical_data() -> None:
    database_url = _database_url()
    previous_url = os.environ.get("DATABASE_URL")
    config = _alembic_config()
    engine = None
    try:
        os.environ["DATABASE_URL"] = database_url
        engine = create_engine(database_url)
        command.downgrade(config, "base")
        command.upgrade(config, "0004_rag_clarification")
        historical_ids = _seed_historical_records(engine)

        command.upgrade(config, "0005_procurement_approval_center")
        values = _seed_principals(engine)
        with engine.begin() as connection:
            instance_id = _insert_instance(connection, values)
            task_id = _insert_task(connection, values, instance_id, sequence=1)
            request_id = _insert_request(connection, values, instance_id)

        command.upgrade(config, "0006_approval_command_operations")
        operation_id = uuid.uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO approval_command_operations "
                    "(id, actor_user_id, client_operation_id, command_kind, instance_id, "
                    "task_id, canonical_payload_hash, status, created_at, updated_at) "
                    "VALUES (:id, :actor_user_id, :client_operation_id, "
                    "'approval.approve', :instance_id, :task_id, :payload_hash, "
                    "'in_progress', now(), now())"
                ),
                {
                    "id": operation_id,
                    "actor_user_id": values["reviewer_user_id"],
                    "client_operation_id": uuid.uuid4(),
                    "instance_id": instance_id,
                    "task_id": task_id,
                    "payload_hash": "f" * 64,
                },
            )

        command.downgrade(config, "0005_procurement_approval_center")
        assert "approval_command_operations" not in set(inspect(engine).get_table_names())
        assert FIFTH_REVISION_TABLES <= set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM procurement_requests WHERE id = :id"),
                {"id": request_id},
            ) == 1
            assert connection.scalar(
                text("SELECT count(*) FROM approval_tasks WHERE id = :id"),
                {"id": task_id},
            ) == 1
        _assert_historical_records(engine, historical_ids)

        command.upgrade(config, "0006_approval_command_operations")
        command.upgrade(config, "head")
        assert "approval_command_operations" in set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM procurement_requests WHERE id = :id"),
                {"id": request_id},
            ) == 1
        _assert_historical_records(engine, historical_ids)

        command.downgrade(config, "0004_rag_clarification")
        assert not (PROCUREMENT_APPROVAL_TABLES & set(inspect(engine).get_table_names()))
        _assert_historical_records(engine, historical_ids)

        command.upgrade(config, "head")
        assert PROCUREMENT_APPROVAL_TABLES <= set(inspect(engine).get_table_names())
        _assert_historical_records(engine, historical_ids)
    finally:
        try:
            if engine is not None:
                command.downgrade(config, "base")
                command.upgrade(config, "head")
        finally:
            if engine is not None:
                engine.dispose()
            if previous_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_url
