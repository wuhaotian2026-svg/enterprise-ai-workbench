from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from policy_api.approvals.enums import (
    ApprovalCommandKind,
    ApprovalCommandStatus,
    ApprovalDecisionAction,
    ApprovalInstanceStatus,
    ApprovalTaskStatus,
    AssignmentKind,
)
from policy_api.models import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


def enum_column(
    enum_type: type,
    *,
    length: int,
    name: str | None = None,
    validate_strings: bool = False,
) -> Enum:
    return Enum(
        enum_type,
        name=name,
        native_enum=False,
        length=length,
        create_constraint=False,
        validate_strings=validate_strings,
        values_callable=lambda items: [item.value for item in items],
    )


class ApprovalInstance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approval_instances"
    __table_args__ = (
        CheckConstraint(
            "(status = 'running' AND current_step_key IS NOT NULL AND completed_at IS NULL) "
            "OR (status IN ('approved', 'rejected', 'cancelled') "
            "AND current_step_key IS NULL AND completed_at IS NOT NULL)",
            name="ck_approval_instance_state_shape",
        ),
        CheckConstraint("version > 0", name="ck_approval_instance_version_positive"),
        Index("ix_approval_instances_applicant_status", "applicant_user_id", "status"),
        Index("ix_approval_instances_organization_status", "organization_unit_id", "status"),
        Index(
            "ix_approval_instances_process_status",
            "process_key",
            "process_version",
            "status",
        ),
        Index("ix_approval_instances_submitted_at", "submitted_at"),
    )

    process_key: Mapped[str] = mapped_column(String(120), nullable=False)
    process_version: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_type: Mapped[str] = mapped_column(String(80), nullable=False)
    applicant_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    organization_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_units.id"), nullable=False
    )
    status: Mapped[ApprovalInstanceStatus] = mapped_column(
        enum_column(ApprovalInstanceStatus, length=9), nullable=False
    )
    current_step_key: Mapped[str | None] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approval_tasks"
    __table_args__ = (
        UniqueConstraint("instance_id", "sequence", name="uq_approval_task_instance_sequence"),
        UniqueConstraint("instance_id", "step_key", name="uq_approval_task_instance_step"),
        UniqueConstraint("id", "instance_id", name="uq_approval_task_id_instance"),
        CheckConstraint(
            "(assignment_kind = 'user' AND assigned_user_id IS NOT NULL "
            "AND required_capability IS NULL AND scope_organization_unit_id IS NULL) OR "
            "(assignment_kind = 'capability' AND assigned_user_id IS NULL "
            "AND required_capability IS NOT NULL)",
            name="ck_approval_task_assignment_shape",
        ),
        CheckConstraint(
            "(status = 'waiting' AND activated_at IS NULL AND completed_at IS NULL) OR "
            "(status = 'pending' AND activated_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status IN ('approved', 'rejected') AND activated_at IS NOT NULL "
            "AND completed_at IS NOT NULL) OR "
            "(status = 'cancelled' AND completed_at IS NOT NULL)",
            name="ck_approval_task_state_shape",
        ),
        Index(
            "uq_approval_tasks_one_pending_per_instance",
            "instance_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_approval_tasks_user_queue",
            "status",
            "assigned_user_id",
            "activated_at",
        ),
        Index(
            "ix_approval_tasks_capability_scope_queue",
            "status",
            "required_capability",
            "scope_organization_unit_id",
            "activated_at",
        ),
    )

    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("approval_instances.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    step_key: Mapped[str] = mapped_column(String(120), nullable=False)
    step_label: Mapped[str] = mapped_column(String(160), nullable=False)
    assignment_kind: Mapped[AssignmentKind] = mapped_column(
        enum_column(AssignmentKind, length=10), nullable=False
    )
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    required_capability: Mapped[str | None] = mapped_column(String(120))
    scope_organization_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_units.id")
    )
    status: Mapped[ApprovalTaskStatus] = mapped_column(
        enum_column(ApprovalTaskStatus, length=9), nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalDecision(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_approval_decision_task"),
        ForeignKeyConstraint(
            ["task_id", "instance_id"],
            ["approval_tasks.id", "approval_tasks.instance_id"],
            name="fk_approval_decision_task_instance",
        ),
        UniqueConstraint(
            "actor_user_id",
            "client_operation_id",
            name="uq_approval_decision_actor_operation",
        ),
        CheckConstraint(
            "action = 'approve' OR (action = 'reject' AND comment IS NOT NULL "
            "AND comment ~ '[^[:space:]]')",
            name="ck_approval_decision_reject_comment",
        ),
    )

    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("approval_instances.id"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    action: Mapped[ApprovalDecisionAction] = mapped_column(
        enum_column(ApprovalDecisionAction, length=7), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(String(500))
    client_operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ApprovalCommandOperation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approval_command_operations"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id",
            "client_operation_id",
            name="uq_approval_command_operation_actor_client_operation",
        ),
        CheckConstraint(
            "command_kind IN ('approval.approve', 'approval.reject', 'approval.cancel')",
            name="ck_approval_command_operation_kind",
        ),
        CheckConstraint(
            "(command_kind IN ('approval.approve', 'approval.reject') "
            "AND task_id IS NOT NULL) OR "
            "(command_kind = 'approval.cancel' AND task_id IS NULL)",
            name="ck_approval_command_operation_task_shape",
        ),
        CheckConstraint(
            "char_length(canonical_payload_hash) = 64",
            name="ck_approval_command_operation_payload_hash",
        ),
        CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL) OR "
            "(status = 'succeeded' AND completed_at IS NOT NULL)",
            name="ck_approval_command_operation_status_shape",
        ),
        ForeignKeyConstraint(
            ["task_id", "instance_id"],
            ["approval_tasks.id", "approval_tasks.instance_id"],
            name="fk_approval_command_operation_task_instance",
        ),
    )

    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    client_operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    command_kind: Mapped[ApprovalCommandKind] = mapped_column(
        enum_column(
            ApprovalCommandKind,
            length=40,
            name="approval_command_kind",
            validate_strings=True,
        ),
        nullable=False,
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("approval_instances.id"), nullable=False
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    canonical_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ApprovalCommandStatus] = mapped_column(
        enum_column(
            ApprovalCommandStatus,
            length=16,
            name="approval_command_status",
            validate_strings=True,
        ),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
