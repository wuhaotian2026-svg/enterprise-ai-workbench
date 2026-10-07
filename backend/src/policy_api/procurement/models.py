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
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from policy_api.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from policy_api.procurement.enums import (
    ProcurementCategoryCode,
    ProcurementCommandOperationStatus,
)


def enum_column(
    enum_type: type,
    *,
    length: int,
    name: str | None = None,
    create_constraint: bool = False,
    validate_strings: bool = False,
) -> Enum:
    return Enum(
        enum_type,
        name=name,
        native_enum=False,
        length=length,
        create_constraint=create_constraint,
        validate_strings=validate_strings,
        values_callable=lambda items: [item.value for item in items],
    )


class ProcurementRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "procurement_requests"
    __table_args__ = (
        CheckConstraint(
            "currency = 'CNY'", name="ck_procurement_request_currency_cny"
        ),
        CheckConstraint(
            "total_amount >= 0", name="ck_procurement_request_total_amount_nonnegative"
        ),
        CheckConstraint(
            "total_amount <= 999999999999.99",
            name="ck_procurement_request_total_amount_maximum",
        ),
        Index(
            "ix_procurement_requests_applicant_submitted",
            "applicant_employee_id",
            "submitted_at",
        ),
        Index(
            "ix_procurement_requests_organization_submitted",
            "organization_unit_id",
            "submitted_at",
        ),
        Index("ix_procurement_requests_needed_by_date", "needed_by_date"),
    )

    request_number: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    approval_instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("approval_instances.id"), nullable=False, unique=True
    )
    applicant_employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employee_profiles.id"), nullable=False
    )
    organization_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_units.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    needed_by_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProcurementRequestItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "procurement_request_items"
    __table_args__ = (
        UniqueConstraint("request_id", "line_number", name="uq_procurement_item_request_line"),
        CheckConstraint(
            "category_code IN ('office_supplies', 'it_equipment', 'software_service', "
            "'professional_service', 'other')",
            name="ck_procurement_request_item_category_code",
        ),
        CheckConstraint(
            "line_number > 0", name="ck_procurement_item_line_number_positive"
        ),
        CheckConstraint("quantity > 0", name="ck_procurement_item_quantity_positive"),
        CheckConstraint(
            "estimated_unit_price >= 0",
            name="ck_procurement_item_unit_price_nonnegative",
        ),
        CheckConstraint("subtotal >= 0", name="ck_procurement_item_subtotal_nonnegative"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("procurement_requests.id"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    category_code: Mapped[ProcurementCategoryCode] = mapped_column(
        enum_column(
            ProcurementCategoryCode,
            length=32,
            name="ck_procurement_request_item_category_code",
            create_constraint=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    item_name: Mapped[str] = mapped_column(String(200), nullable=False)
    specification: Mapped[str | None] = mapped_column(String(500))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    estimated_unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)


class ProcurementCommandOperation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "procurement_command_operations"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id",
            "client_operation_id",
            name="uq_procurement_operation_actor_client_operation",
        ),
        CheckConstraint(
            "(status = 'in_progress' AND result_resource_type IS NULL "
            "AND result_resource_id IS NULL) OR "
            "(status = 'succeeded' AND result_resource_type IS NOT NULL "
            "AND result_resource_id IS NOT NULL)",
            name="ck_procurement_operation_result_shape",
        ),
    )

    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    client_operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    command_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    canonical_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ProcurementCommandOperationStatus] = mapped_column(
        enum_column(ProcurementCommandOperationStatus, length=11), nullable=False
    )
    result_resource_type: Mapped[str | None] = mapped_column(String(80))
    result_resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class AssistantConversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assistant_conversations"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "owner_user_id",
            "module_key",
            name="uq_assistant_conversation_id_owner_module",
        ),
        Index(
            "ix_assistant_conversations_owner_module_updated",
            "owner_user_id",
            "module_key",
            "updated_at",
        ),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    module_key: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AssistantTurn(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assistant_turns"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "module_key",
            "client_turn_id",
            name="uq_assistant_turn_owner_module_client_turn",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "owner_user_id", "module_key"],
            [
                "assistant_conversations.id",
                "assistant_conversations.owner_user_id",
                "assistant_conversations.module_key",
            ],
            name="fk_assistant_turn_conversation_owner_module",
        ),
        Index("ix_assistant_turns_conversation_created", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    module_key: Mapped[str] = mapped_column(String(80), nullable=False)
    client_turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    request_content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    blocks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(120))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
