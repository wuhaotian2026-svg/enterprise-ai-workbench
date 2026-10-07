from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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

from policy_api.models import Base, StringEnum, TimestampMixin, UUIDPrimaryKeyMixin


class SlotExtractionOperationStatus(StringEnum):
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


class SlotExtractionOperation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assistant_slot_extraction_operations"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "module_key",
            "client_turn_id",
            name="uq_slot_extraction_operation_owner_module_turn",
        ),
        CheckConstraint(
            "module_key IN ('hr', 'procurement')",
            name="ck_slot_extraction_operation_module",
        ),
        CheckConstraint(
            "status IN ('reserved', 'dispatched', 'succeeded', 'failed', "
            "'indeterminate')",
            name="ck_slot_extraction_operation_status",
        ),
        CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name="ck_slot_extraction_operation_request_fingerprint",
        ),
        CheckConstraint(
            "char_length(slot_schema_sha256) = 64",
            name="ck_slot_extraction_operation_schema_sha256",
        ),
        CheckConstraint(
            "accepted_count >= 0 AND pending_count >= 0 AND rejected_count >= 0",
            name="ck_slot_extraction_operation_counts_nonnegative",
        ),
        CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'indeterminate') "
            "OR completed_at IS NOT NULL",
            name="ck_slot_extraction_operation_terminal_completed",
        ),
        CheckConstraint(
            "status != 'dispatched' OR dispatched_at IS NOT NULL",
            name="ck_slot_extraction_operation_dispatched_at",
        ),
        Index(
            "ix_slot_extraction_operations_status_deadline",
            "status",
            "deadline_at",
        ),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    module_key: Mapped[str] = mapped_column(String(20), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    client_turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_key_id: Mapped[str] = mapped_column(String(80), nullable=False)
    slot_schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    slot_schema_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[SlotExtractionOperationStatus] = mapped_column(
        Enum(
            SlotExtractionOperationStatus,
            native_enum=False,
            length=16,
            create_constraint=False,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
