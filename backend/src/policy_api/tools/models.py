from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from policy_api.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from policy_api.tools.enums import (
    ToolAuditEventKind,
    ToolConfirmationStatus,
    ToolInvocationStatus,
)


def enum_column(enum_type: type, *, length: int) -> Enum:
    return Enum(
        enum_type,
        native_enum=False,
        length=length,
        values_callable=lambda items: [item.value for item in items],
    )


class ToolInvocation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tool_invocations"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "provider_call_id",
            name="uq_tool_invocation_conversation_provider_call",
        ),
        Index("ix_tool_invocations_actor_created", "actor_user_id", "created_at"),
        Index("ix_tool_invocations_turn", "turn_id"),
        Index("ix_tool_invocations_tool_status", "tool_name", "status"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    provider_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[ToolInvocationStatus] = mapped_column(
        enum_column(ToolInvocationStatus, length=20), nullable=False
    )
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_resource_type: Mapped[str | None] = mapped_column(String(80))
    result_resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))


class ToolConfirmation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tool_confirmations"
    __table_args__ = (
        UniqueConstraint("invocation_id", name="uq_tool_confirmation_invocation"),
        UniqueConstraint(
            "owner_user_id",
            "client_operation_id",
            name="uq_tool_confirmation_owner_operation",
        ),
        CheckConstraint(
            "("
            "status = 'pending' AND consumed_at IS NULL AND cancelled_at IS NULL"
            ") OR ("
            "status = 'consumed' AND consumed_at IS NOT NULL AND cancelled_at IS NULL"
            ") OR ("
            "status = 'cancelled' AND consumed_at IS NULL AND cancelled_at IS NOT NULL"
            ") OR ("
            "status = 'expired' AND consumed_at IS NULL AND cancelled_at IS NULL"
            ")",
            name="ck_tool_confirmation_status_shape",
        ),
        Index(
            "ix_tool_confirmations_owner_status_expiry",
            "owner_user_id",
            "status",
            "expires_at",
        ),
    )

    invocation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tool_invocations.id"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_arguments: Mapped[dict] = mapped_column(JSON, nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[ToolConfirmationStatus] = mapped_column(
        enum_column(ToolConfirmationStatus, length=9), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    result_resource_type: Mapped[str | None] = mapped_column(String(80))
    result_resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ToolAuditEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tool_audit_events"
    __table_args__ = (
        Index("ix_tool_audit_invocation_created", "invocation_id", "created_at"),
        Index("ix_tool_audit_confirmation_created", "confirmation_id", "created_at"),
    )

    invocation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tool_invocations.id"), nullable=False
    )
    confirmation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tool_confirmations.id")
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    event_kind: Mapped[ToolAuditEventKind] = mapped_column(
        enum_column(ToolAuditEventKind, length=21), nullable=False
    )
    summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
