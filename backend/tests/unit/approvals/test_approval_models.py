from __future__ import annotations

from enum import Enum

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.sqltypes import NullType

from policy_api.approvals.enums import (
    ApprovalCommandKind,
    ApprovalCommandStatus,
    ApprovalDecisionAction,
    ApprovalInstanceStatus,
    ApprovalTaskStatus,
    AssignmentKind,
)
from policy_api.approvals.models import (
    ApprovalCommandOperation,
    ApprovalDecision,
    ApprovalInstance,
    ApprovalTask,
)
from policy_api.models import Base


def enum_values(enum_type: type[Enum]) -> set[str]:
    return {item.value for item in enum_type}


def unique_column_sets(model: type[Base]) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def check_sql(model: type[Base], name: str) -> str:
    constraint = next(
        constraint
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == name
    )
    return " ".join(str(constraint.sqltext).split())


def index_by_name(model: type[Base], name: str) -> Index:
    return next(index for index in model.__table__.indexes if index.name == name)


def foreign_key_targets(model: type[Base], column_name: str) -> set[str]:
    return {
        foreign_key.target_fullname
        for foreign_key in model.__table__.columns[column_name].foreign_keys
    }


def test_approval_enums_are_closed_to_the_frozen_values() -> None:
    assert enum_values(ApprovalInstanceStatus) == {
        "running",
        "approved",
        "rejected",
        "cancelled",
    }
    assert enum_values(ApprovalTaskStatus) == {
        "waiting",
        "pending",
        "approved",
        "rejected",
        "cancelled",
    }
    assert enum_values(ApprovalDecisionAction) == {"approve", "reject"}
    assert enum_values(AssignmentKind) == {"user", "capability"}
    assert enum_values(ApprovalCommandKind) == {
        "approval.approve",
        "approval.reject",
        "approval.cancel",
    }
    assert enum_values(ApprovalCommandStatus) == {"in_progress", "succeeded"}


def test_approval_tables_join_shared_metadata() -> None:
    assert {
        "approval_instances",
        "approval_tasks",
        "approval_decisions",
        "approval_command_operations",
    } <= set(Base.metadata.tables)


def test_approval_command_operation_freezes_low_sensitivity_idempotency_shape() -> None:
    columns = ApprovalCommandOperation.__table__.columns
    assert set(columns.keys()) == {
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
    for column_name in (
        "id",
        "actor_user_id",
        "client_operation_id",
        "command_kind",
        "instance_id",
        "canonical_payload_hash",
        "status",
        "created_at",
        "updated_at",
    ):
        assert columns[column_name].nullable is False
    assert columns["task_id"].nullable is True
    assert columns["completed_at"].nullable is True
    assert columns["command_kind"].type.length == 40
    assert columns["canonical_payload_hash"].type.length == 64
    assert columns["status"].type.length == 16
    assert foreign_key_targets(ApprovalCommandOperation, "actor_user_id") == {"users.id"}
    assert "approval_instances.id" in foreign_key_targets(
        ApprovalCommandOperation, "instance_id"
    )
    assert foreign_key_targets(ApprovalCommandOperation, "task_id") == {
        "approval_tasks.id"
    }
    assert ("actor_user_id", "client_operation_id") in unique_column_sets(
        ApprovalCommandOperation
    )
    assert check_sql(
        ApprovalCommandOperation, "ck_approval_command_operation_kind"
    ) == (
        "command_kind IN ('approval.approve', 'approval.reject', 'approval.cancel')"
    )
    assert check_sql(
        ApprovalCommandOperation, "ck_approval_command_operation_task_shape"
    ) == (
        "(command_kind IN ('approval.approve', 'approval.reject') AND task_id IS NOT NULL) "
        "OR (command_kind = 'approval.cancel' AND task_id IS NULL)"
    )
    assert check_sql(
        ApprovalCommandOperation, "ck_approval_command_operation_payload_hash"
    ) == "char_length(canonical_payload_hash) = 64"
    assert check_sql(
        ApprovalCommandOperation, "ck_approval_command_operation_status_shape"
    ) == (
        "(status = 'in_progress' AND completed_at IS NULL) OR "
        "(status = 'succeeded' AND completed_at IS NOT NULL)"
    )
    task_instance_link = next(
        constraint
        for constraint in ApprovalCommandOperation.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and tuple(column.name for column in constraint.columns) == ("task_id", "instance_id")
    )
    assert task_instance_link.name == "fk_approval_command_operation_task_instance"
    assert tuple(element.target_fullname for element in task_instance_link.elements) == (
        "approval_tasks.id",
        "approval_tasks.instance_id",
    )


def test_approval_command_enum_columns_bind_only_canonical_values() -> None:
    dialect = postgresql.dialect()
    for column_name, expected_values, valid_value in (
        (
            "command_kind",
            ["approval.approve", "approval.reject", "approval.cancel"],
            "approval.approve",
        ),
        ("status", ["in_progress", "succeeded"], "in_progress"),
    ):
        column_type = ApprovalCommandOperation.__table__.columns[column_name].type
        assert column_type.enums == expected_values
        processor = column_type.bind_processor(dialect)
        assert processor is not None
        assert processor(valid_value) == valid_value
        with pytest.raises(LookupError):
            processor("not-a-canonical-value")


def test_approval_instance_uses_process_subject_and_user_contract() -> None:
    columns = ApprovalInstance.__table__.columns
    assert "applicant_employee_id" not in columns
    for column_name in (
        "process_key",
        "process_version",
        "subject_type",
        "applicant_user_id",
        "organization_unit_id",
        "status",
        "version",
        "submitted_at",
    ):
        assert columns[column_name].nullable is False
    assert columns["current_step_key"].nullable is True
    assert columns["completed_at"].nullable is True
    assert isinstance(columns["process_key"].type, String)
    assert columns["process_key"].type.length == 120
    assert columns["subject_type"].type.length == 80
    assert foreign_key_targets(ApprovalInstance, "applicant_user_id") == {"users.id"}
    assert foreign_key_targets(ApprovalInstance, "organization_unit_id") == {
        "organization_units.id"
    }
    assert check_sql(ApprovalInstance, "ck_approval_instance_state_shape") == (
        "(status = 'running' AND current_step_key IS NOT NULL AND completed_at IS NULL) "
        "OR (status IN ('approved', 'rejected', 'cancelled') "
        "AND current_step_key IS NULL AND completed_at IS NOT NULL)"
    )
    assert check_sql(ApprovalInstance, "ck_approval_instance_version_positive") == "version > 0"

    applicant_index = index_by_name(ApprovalInstance, "ix_approval_instances_applicant_status")
    organization_index = index_by_name(
        ApprovalInstance, "ix_approval_instances_organization_status"
    )
    process_index = index_by_name(ApprovalInstance, "ix_approval_instances_process_status")
    submitted_index = index_by_name(ApprovalInstance, "ix_approval_instances_submitted_at")
    assert tuple(column.name for column in applicant_index.columns) == (
        "applicant_user_id",
        "status",
    )
    assert tuple(column.name for column in organization_index.columns) == (
        "organization_unit_id",
        "status",
    )
    assert tuple(column.name for column in process_index.columns) == (
        "process_key",
        "process_version",
        "status",
    )
    assert tuple(column.name for column in submitted_index.columns) == ("submitted_at",)


def test_approval_task_snapshots_assignment_scope_and_state_shapes() -> None:
    columns = ApprovalTask.__table__.columns
    for column_name in (
        "instance_id",
        "sequence",
        "step_key",
        "step_label",
        "assignment_kind",
        "status",
    ):
        assert columns[column_name].nullable is False
    for column_name in (
        "assigned_user_id",
        "required_capability",
        "scope_organization_unit_id",
        "activated_at",
        "completed_at",
    ):
        assert columns[column_name].nullable is True
    assert "assigned_capability" not in columns
    assert columns["step_label"].type.length == 160
    assert columns["required_capability"].type.length == 120
    assert foreign_key_targets(ApprovalTask, "instance_id") == {"approval_instances.id"}
    assert foreign_key_targets(ApprovalTask, "assigned_user_id") == {"users.id"}
    assert foreign_key_targets(ApprovalTask, "scope_organization_unit_id") == {
        "organization_units.id"
    }
    assert {
        ("instance_id", "sequence"),
        ("instance_id", "step_key"),
        ("id", "instance_id"),
    } <= unique_column_sets(ApprovalTask)
    assert check_sql(ApprovalTask, "ck_approval_task_assignment_shape") == (
        "(assignment_kind = 'user' AND assigned_user_id IS NOT NULL "
        "AND required_capability IS NULL AND scope_organization_unit_id IS NULL) OR "
        "(assignment_kind = 'capability' AND assigned_user_id IS NULL "
        "AND required_capability IS NOT NULL)"
    )
    assert check_sql(ApprovalTask, "ck_approval_task_state_shape") == (
        "(status = 'waiting' AND activated_at IS NULL AND completed_at IS NULL) OR "
        "(status = 'pending' AND activated_at IS NOT NULL AND completed_at IS NULL) OR "
        "(status IN ('approved', 'rejected') AND activated_at IS NOT NULL "
        "AND completed_at IS NOT NULL) OR "
        "(status = 'cancelled' AND completed_at IS NOT NULL)"
    )
    pending_index = index_by_name(
        ApprovalTask, "uq_approval_tasks_one_pending_per_instance"
    )
    assert pending_index.unique is True
    assert tuple(column.name for column in pending_index.columns) == ("instance_id",)
    assert str(pending_index.dialect_options["postgresql"]["where"]) == "status = 'pending'"
    user_queue_index = index_by_name(ApprovalTask, "ix_approval_tasks_user_queue")
    capability_queue_index = index_by_name(
        ApprovalTask, "ix_approval_tasks_capability_scope_queue"
    )
    assert tuple(column.name for column in user_queue_index.columns) == (
        "status",
        "assigned_user_id",
        "activated_at",
    )
    assert tuple(column.name for column in capability_queue_index.columns) == (
        "status",
        "required_capability",
        "scope_organization_unit_id",
        "activated_at",
    )


def test_approval_decision_is_append_only_with_actor_scoped_idempotency() -> None:
    columns = ApprovalDecision.__table__.columns
    assert "updated_at" not in columns
    for column_name in (
        "instance_id",
        "task_id",
        "actor_user_id",
        "action",
        "client_operation_id",
        "decided_at",
        "created_at",
    ):
        assert columns[column_name].nullable is False
    assert columns["comment"].nullable is True
    assert columns["comment"].type.length == 500
    assert "approval_instances.id" in foreign_key_targets(ApprovalDecision, "instance_id")
    assert foreign_key_targets(ApprovalDecision, "task_id") == {"approval_tasks.id"}
    assert foreign_key_targets(ApprovalDecision, "actor_user_id") == {"users.id"}
    assert {
        ("task_id",),
        ("actor_user_id", "client_operation_id"),
    } <= unique_column_sets(ApprovalDecision)
    assert check_sql(ApprovalDecision, "ck_approval_decision_reject_comment") == (
        "action = 'approve' OR (action = 'reject' AND comment IS NOT NULL "
        "AND comment ~ '[^[:space:]]')"
    )
    task_instance_link = next(
        constraint
        for constraint in ApprovalDecision.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and tuple(column.name for column in constraint.columns) == ("task_id", "instance_id")
    )
    assert tuple(element.target_fullname for element in task_instance_link.elements) == (
        "approval_tasks.id",
        "approval_tasks.instance_id",
    )


def test_approval_foreign_key_columns_have_explicit_uuid_types() -> None:
    for model in (
        ApprovalInstance,
        ApprovalTask,
        ApprovalDecision,
        ApprovalCommandOperation,
    ):
        for column in model.__table__.columns:
            if column.foreign_keys:
                assert not isinstance(column.type, NullType), f"{model.__name__}.{column.name}"
