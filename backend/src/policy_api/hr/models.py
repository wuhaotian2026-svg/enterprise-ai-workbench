from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from policy_api.hr.enums import (
    LeaveAccountEventKind,
    LeaveRequestStatus,
    LeaveTypeCode,
    WorkCalendarDayKind,
)
from policy_api.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


def enum_column(enum_type: type, *, length: int) -> Enum:
    return Enum(
        enum_type,
        native_enum=False,
        length=length,
        values_callable=lambda items: [item.value for item in items],
    )


class EmployeeProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employee_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_employee_profile_user"),
        CheckConstraint(
            "manager_employee_id IS NULL OR manager_employee_id <> id",
            name="ck_employee_profile_manager_not_self",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    employee_number: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    department: Mapped[str | None] = mapped_column(String(120))
    organization_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organization_units.id")
    )
    manager_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employee_profiles.id")
    )
    hire_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LeaveType(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leave_types"

    code: Mapped[LeaveTypeCode] = mapped_column(
        enum_column(LeaveTypeCode, length=12), nullable=False, unique=True
    )
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LeaveAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leave_accounts"
    __table_args__ = (
        UniqueConstraint(
            "employee_id",
            "leave_type_id",
            "year",
            name="uq_leave_account_employee_type_year",
        ),
        CheckConstraint("entitled >= 0", name="ck_leave_account_entitled_nonnegative"),
        CheckConstraint("used >= 0", name="ck_leave_account_used_nonnegative"),
        CheckConstraint("reserved >= 0", name="ck_leave_account_reserved_nonnegative"),
        CheckConstraint(
            "used + reserved <= entitled",
            name="ck_leave_account_within_entitlement",
        ),
        Index("ix_leave_accounts_employee_year", "employee_id", "year"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee_profiles.id"), nullable=False
    )
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave_types.id"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    entitled: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), default=Decimal("0"), nullable=False
    )
    used: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), default=Decimal("0"), nullable=False
    )
    reserved: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), default=Decimal("0"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class WorkCalendarDay(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "work_calendar_days"

    calendar_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    kind: Mapped[WorkCalendarDayKind] = mapped_column(
        enum_column(WorkCalendarDayKind, length=16), nullable=False
    )
    is_workday: Mapped[bool] = mapped_column(Boolean, nullable=False)
    label: Mapped[str | None] = mapped_column(String(120))


class LeaveRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leave_requests"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_leave_request_dates_valid"),
        CheckConstraint(
            "workday_count > 0", name="ck_leave_request_workdays_positive"
        ),
        CheckConstraint(
            "("
            "status = 'pending' AND reviewer_user_id IS NULL AND reviewed_at IS NULL "
            "AND rejection_reason IS NULL AND cancelled_at IS NULL "
            "AND cancelled_by_user_id IS NULL"
            ") OR ("
            "status = 'approved' AND reviewer_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND rejection_reason IS NULL "
            "AND cancelled_at IS NULL AND cancelled_by_user_id IS NULL"
            ") OR ("
            "status = 'rejected' AND reviewer_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND rejection_reason IS NOT NULL "
            "AND cancelled_at IS NULL AND cancelled_by_user_id IS NULL"
            ") OR ("
            "status = 'cancelled' AND reviewer_user_id IS NULL "
            "AND reviewed_at IS NULL AND rejection_reason IS NULL "
            "AND cancelled_at IS NOT NULL AND cancelled_by_user_id IS NOT NULL"
            ")",
            name="ck_leave_request_state_shape",
        ),
        Index("ix_leave_requests_employee_status", "employee_id", "status"),
        Index(
            "ix_leave_requests_employee_dates",
            "employee_id",
            "start_date",
            "end_date",
        ),
        Index("ix_leave_requests_review_queue", "status", "submitted_at"),
    )

    request_number: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee_profiles.id"), nullable=False
    )
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave_types.id"), nullable=False
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    workday_count: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[LeaveRequestStatus] = mapped_column(
        enum_column(LeaveRequestStatus, length=9), nullable=False
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class LeaveAccountEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leave_account_events"
    __table_args__ = (
        UniqueConstraint(
            "operation_id", name="uq_leave_account_event_operation"
        ),
        CheckConstraint("used_after >= 0", name="ck_leave_event_used_nonnegative"),
        CheckConstraint(
            "reserved_after >= 0", name="ck_leave_event_reserved_nonnegative"
        ),
        Index("ix_leave_account_events_account_created", "account_id", "created_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave_accounts.id"), nullable=False
    )
    leave_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leave_requests.id")
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[LeaveAccountEventKind] = mapped_column(
        enum_column(LeaveAccountEventKind, length=7), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    used_after: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    reserved_after: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)


class HrConversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "hr_conversations"
    __table_args__ = (
        Index("ix_hr_conversations_owner_updated", "owner_user_id", "updated_at"),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class HrTurn(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "hr_turns"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "client_turn_id", name="uq_hr_turn_owner_client_turn"
        ),
        Index("ix_hr_turns_conversation_created", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hr_conversations.id"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    client_turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    blocks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(120))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
