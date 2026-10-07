from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from policy_api.models import Base
from policy_api.tools.models import ToolAuditEvent, ToolConfirmation, ToolInvocation


def unique_column_sets(model: type[Base]) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def check_names(model: type[Base]) -> set[str | None]:
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_tool_tables_join_shared_metadata_and_preserve_double_idempotency() -> None:
    assert {"tool_invocations", "tool_confirmations", "tool_audit_events"} <= set(
        Base.metadata.tables
    )
    assert ("conversation_id", "provider_call_id") in unique_column_sets(ToolInvocation)
    assert ("invocation_id",) in unique_column_sets(ToolConfirmation)
    assert ("owner_user_id", "client_operation_id") in unique_column_sets(
        ToolConfirmation
    )


def test_confirmation_has_a_named_status_timestamp_shape_constraint() -> None:
    assert "ck_tool_confirmation_status_shape" in check_names(ToolConfirmation)
    columns = ToolConfirmation.__table__.columns
    assert columns["normalized_arguments"].nullable is False
    assert columns["arguments_hash"].nullable is False
    assert columns["preview"].nullable is False
    assert columns["expires_at"].nullable is False


def test_audit_event_can_link_confirmation_but_never_requires_message_content() -> None:
    columns = ToolAuditEvent.__table__.columns
    assert columns["invocation_id"].nullable is False
    assert columns["confirmation_id"].nullable is True
    assert columns["actor_user_id"].nullable is False
    assert "message" not in columns
    assert "reason" not in columns
