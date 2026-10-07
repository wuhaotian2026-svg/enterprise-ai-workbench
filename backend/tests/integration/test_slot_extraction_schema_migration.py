from __future__ import annotations

import importlib
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

from policy_api.database import assert_test_database_url
from policy_api.models import Base


EXPECTED_COLUMNS = {
    "id",
    "owner_user_id",
    "module_key",
    "conversation_id",
    "client_turn_id",
    "request_fingerprint",
    "fingerprint_key_id",
    "slot_schema_version",
    "slot_schema_sha256",
    "model_name",
    "status",
    "deadline_at",
    "dispatched_at",
    "completed_at",
    "accepted_count",
    "pending_count",
    "rejected_count",
    "error_code",
    "latency_ms",
    "created_at",
    "updated_at",
}


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _migration_path() -> Path:
    return (
        _backend_root()
        / "alembic"
        / "versions"
        / "0008_slot_extraction_operations.py"
    )


def _alembic_config() -> Config:
    config = Config(str(_backend_root() / "alembic.ini"))
    config.set_main_option("script_location", str(_backend_root() / "alembic"))
    return config


def test_0008_is_explicit_single_table_migration() -> None:
    source = _migration_path().read_text(encoding="utf-8")

    assert 'revision = "0008_slot_extraction_operations"' in source
    assert 'down_revision = "0007_assistant_flow_drafts"' in source
    assert "from policy_api.models import Base" not in source
    assert "metadata.create_all" not in source
    assert "metadata.drop_all" not in source
    assert set(
        re.findall(r'op\.create_table\(\s*["\']([^"\']+)["\']', source)
    ) == {"assistant_slot_extraction_operations"}
    assert 'op.drop_table("assistant_slot_extraction_operations")' in source


def test_alembic_env_registers_slot_extraction_models() -> None:
    source = (_backend_root() / "alembic" / "env.py").read_text(encoding="utf-8")

    assert "policy_api.slot_extraction import models as _slot_extraction_models" in source


def test_metadata_exposes_frozen_ledger_contract() -> None:
    module = importlib.import_module("policy_api.slot_extraction.models")
    table = Base.metadata.tables["assistant_slot_extraction_operations"]

    assert set(table.columns.keys()) == EXPECTED_COLUMNS
    assert module.SlotExtractionOperationStatus.INDETERMINATE.value == "indeterminate"
    assert {
        constraint.name for constraint in table.constraints if constraint.name is not None
    } >= {
        "uq_slot_extraction_operation_owner_module_turn",
        "ck_slot_extraction_operation_module",
        "ck_slot_extraction_operation_status",
        "ck_slot_extraction_operation_request_fingerprint",
        "ck_slot_extraction_operation_schema_sha256",
        "ck_slot_extraction_operation_counts_nonnegative",
        "ck_slot_extraction_operation_terminal_completed",
        "ck_slot_extraction_operation_dispatched_at",
    }


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for slot extraction migration verification",
)
def test_head_columns_constraints_and_metadata_have_no_drift() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    assert_test_database_url(database_url)
    previous_url = os.environ.get("DATABASE_URL")
    config = _alembic_config()
    engine = create_engine(database_url)
    try:
        os.environ["DATABASE_URL"] = database_url
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert "assistant_slot_extraction_operations" in inspector.get_table_names()
        assert {
            column["name"]
            for column in inspector.get_columns("assistant_slot_extraction_operations")
        } == EXPECTED_COLUMNS
        assert {
            item["name"]
            for item in inspector.get_unique_constraints(
                "assistant_slot_extraction_operations"
            )
        } == {"uq_slot_extraction_operation_owner_module_turn"}

        importlib.import_module("policy_api.slot_extraction.models")

        def include_ledger_object(
            object_: object,
            name: str | None,
            type_: str,
            _reflected: bool,
            _compare_to: object | None,
        ) -> bool:
            if type_ == "table":
                return name == "assistant_slot_extraction_operations"
            table = getattr(object_, "table", None)
            return (
                getattr(table, "name", None)
                == "assistant_slot_extraction_operations"
            )

        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"include_object": include_ledger_object},
            )
            assert compare_metadata(context, Base.metadata) == []

        owner_user_id = uuid.uuid4()
        valid_values = {
            "id": uuid.uuid4(),
            "owner_user_id": owner_user_id,
            "conversation_id": uuid.uuid4(),
            "client_turn_id": uuid.uuid4(),
            "request_fingerprint": "a" * 64,
            "slot_schema_sha256": "b" * 64,
        }
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, password_hash, role, is_active, created_at, updated_at) "
                    "VALUES (:id, :username, 'hash', 'employee', true, now(), now())"
                ),
                {"id": owner_user_id, "username": f"slot-owner-{owner_user_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO assistant_slot_extraction_operations "
                    "(id, owner_user_id, module_key, conversation_id, client_turn_id, "
                    "request_fingerprint, fingerprint_key_id, slot_schema_version, "
                    "slot_schema_sha256, model_name, status, deadline_at, dispatched_at, "
                    "accepted_count, pending_count, rejected_count, created_at, updated_at) "
                    "VALUES (:id, :owner_user_id, 'hr', :conversation_id, :client_turn_id, "
                    ":request_fingerprint, 'session-secret-hmac-sha256-v1', "
                    "'slot-extraction-v1', :slot_schema_sha256, 'deepseek-v4-flash', "
                    "'dispatched', now(), now(), 0, 0, 0, now(), now())"
                ),
                valid_values,
            )

        invalid_statements = (
            "UPDATE assistant_slot_extraction_operations SET module_key = 'finance'",
            "UPDATE assistant_slot_extraction_operations SET request_fingerprint = 'x'",
            "UPDATE assistant_slot_extraction_operations SET accepted_count = -1",
            "UPDATE assistant_slot_extraction_operations "
            "SET status = 'succeeded', completed_at = NULL",
            "UPDATE assistant_slot_extraction_operations "
            "SET status = 'dispatched', dispatched_at = NULL",
        )
        for statement in invalid_statements:
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(text(statement))
    finally:
        try:
            command.upgrade(config, "head")
        finally:
            engine.dispose()
            if previous_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_url
