from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy import select
from sqlalchemy.orm import Mapped, Session, mapped_column

from policy_api.hr.enums import LeaveTypeCode
from policy_api.models import Base, UUIDPrimaryKeyMixin, utc_now
from policy_api.workbench.catalog import DEFAULT_MODULE_KEYS


class ProductEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "product_events"
    __table_args__ = (
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_product_event_duration_nonnegative",
        ),
        Index("ix_product_events_name_occurred", "event_name", "occurred_at"),
        Index("ix_product_events_module_occurred", "module_key", "occurred_at"),
        Index("ix_product_events_actor_occurred", "actor_user_id", "occurred_at"),
        Index(
            "ix_product_events_organization_occurred",
            "organization_unit_id",
            "occurred_at",
        ),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False
    )
    event_name: Mapped[str] = mapped_column(String(80), nullable=False)
    module_key: Mapped[str] = mapped_column(String(60), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    organization_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organization_units.id")
    )
    role_snapshot: Mapped[str] = mapped_column(String(24), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(120))
    outcome: Mapped[str | None] = mapped_column(String(80))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    dimensions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


MessageLengthBucket = Literal["0_50", "51_200", "201_500", "501_plus"]
ProcessingTimeBucket = Literal[
    "lt_1s", "1s_3s", "3s_10s", "gte_10s", "same_day", "later",
]
ProcurementProcessingTimeBucket = Literal[
    "lt_1s", "1s_3s", "3s_10s", "10s_24h", "24h_48h", "gte_48h",
]
ToolName = Literal[
    "knowledge.search_policy",
    "hr.get_my_leave_balances",
    "hr.list_my_leave_requests",
    "hr.get_my_leave_request",
    "hr.calculate_leave_duration",
    "hr.submit_leave_request",
    "hr.cancel_leave_request",
    "procurement.submit_request",
    "procurement.withdraw_request",
    "approval.approve_task",
    "approval.reject_task",
]
StableCode = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]*$"),
]


class ClosedDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WorkbenchModuleOpenedDimensions(ClosedDimensions):
    entry_source: Literal["navigation"]
    module_key: Literal[
        "knowledge",
        "hr-assistant",
        "my-requests",
        "hr-review",
        "knowledge-admin",
        "organization",
        "analytics",
        "procurement",
        "approval-center",
    ]


class QuestionSubmittedDimensions(ClosedDimensions):
    message_length_bucket: MessageLengthBucket


class QuestionAnsweredDimensions(ClosedDimensions):
    has_citations: bool
    processing_time_bucket: ProcessingTimeBucket


class QuestionClarificationRequestedDimensions(ClosedDimensions):
    question_count_bucket: Literal["one", "two", "three"]
    has_citations: Literal[True]
    processing_time_bucket: ProcessingTimeBucket


class QuestionAbstainedDimensions(ClosedDimensions):
    error_code: StableCode


class AnswerFeedbackSubmittedDimensions(ClosedDimensions):
    helpful: bool


class HrTurnSubmittedDimensions(ClosedDimensions):
    message_length_bucket: MessageLengthBucket


class HrIntentResolvedDimensions(ClosedDimensions):
    intent: Literal[
        "get_my_leave_balances",
        "get_leave_policy",
        "list_my_leave_requests",
        "get_my_leave_request",
        "calculate_leave_duration",
        "submit_leave_request",
        "cancel_leave_request",
        "unknown",
    ]
    clarification_required: bool


class ToolPlannedDimensions(ClosedDimensions):
    tool_name: ToolName
    risk_level: Literal["read", "sensitive_read", "write"]


class ToolValidationFailedDimensions(ClosedDimensions):
    tool_name: ToolName
    error_code: StableCode
    retryable: bool


class ToolReadSucceededDimensions(ClosedDimensions):
    tool_name: ToolName
    processing_time_bucket: ProcessingTimeBucket


class ConfirmationShownDimensions(ClosedDimensions):
    tool_name: ToolName
    risk_level: Literal["write"]


class ConfirmationConfirmedDimensions(ClosedDimensions):
    tool_name: ToolName


class ConfirmationCancelledDimensions(ClosedDimensions):
    tool_name: ToolName


class ConfirmationExpiredDimensions(ClosedDimensions):
    tool_name: ToolName


class LeaveTypeDimensions(ClosedDimensions):
    leave_type: LeaveTypeCode

    @field_validator("leave_type", mode="before")
    @classmethod
    def validate_leave_type(cls, value: object) -> LeaveTypeCode:
        if isinstance(value, LeaveTypeCode):
            return value
        try:
            return LeaveTypeCode(value)
        except (TypeError, ValueError):
            raise ValueError("leave_type_invalid") from None


class LeaveRequestSubmittedDimensions(LeaveTypeDimensions):
    workday_count_bucket: Literal["0_1", "1_2", "3_5", "6_plus"]


class LeaveRequestReviewedDimensions(ClosedDimensions):
    decision: Literal["approved", "rejected"]
    processing_time_bucket: ProcessingTimeBucket


class LeaveRequestCancelledDimensions(LeaveTypeDimensions):
    pass


class HrFlowErrorDimensions(ClosedDimensions):
    error_code: StableCode
    retryable: bool


class ProcurementRequestSubmittedDimensions(ClosedDimensions):
    stage: Literal["submission"]
    channel: Literal["manual", "ai_confirmation"]
    item_count_bucket: Literal["1", "2_5", "6_10", "11_plus"]
    amount_bucket: Literal[
        "0_999", "1000_9999", "10000_99999", "100000_plus"
    ]


class ProcurementRequestWithdrawnDimensions(ClosedDimensions):
    stage: Literal["withdrawal"]
    processing_time_bucket: ProcurementProcessingTimeBucket


class ApprovalTaskApprovedDimensions(ClosedDimensions):
    stage: Literal["department_review", "procurement_review"]
    processing_time_bucket: ProcurementProcessingTimeBucket


class ApprovalTaskRejectedDimensions(ClosedDimensions):
    stage: Literal["department_review", "procurement_review"]
    processing_time_bucket: ProcurementProcessingTimeBucket


class ProcurementRequestCompletedDimensions(ClosedDimensions):
    stage: Literal["completion"]
    outcome: Literal["approved", "rejected", "withdrawn"]
    processing_time_bucket: ProcurementProcessingTimeBucket


class ProcurementFlowErrorDimensions(ClosedDimensions):
    stage: Literal[
        "submission", "withdrawal", "department_review",
        "procurement_review", "confirmation"
    ]
    error_code: StableCode
    outcome: Literal["conflict", "denied"]


EVENT_DIMENSION_MODELS: dict[str, type[ClosedDimensions]] = {
    "workbench_module_opened": WorkbenchModuleOpenedDimensions,
    "question_submitted": QuestionSubmittedDimensions,
    "question_answered": QuestionAnsweredDimensions,
    "question_clarification_requested": QuestionClarificationRequestedDimensions,
    "question_abstained": QuestionAbstainedDimensions,
    "answer_feedback_submitted": AnswerFeedbackSubmittedDimensions,
    "hr_turn_submitted": HrTurnSubmittedDimensions,
    "hr_intent_resolved": HrIntentResolvedDimensions,
    "tool_planned": ToolPlannedDimensions,
    "tool_validation_failed": ToolValidationFailedDimensions,
    "tool_read_succeeded": ToolReadSucceededDimensions,
    "confirmation_shown": ConfirmationShownDimensions,
    "confirmation_confirmed": ConfirmationConfirmedDimensions,
    "confirmation_cancelled": ConfirmationCancelledDimensions,
    "confirmation_expired": ConfirmationExpiredDimensions,
    "leave_request_submitted": LeaveRequestSubmittedDimensions,
    "leave_request_reviewed": LeaveRequestReviewedDimensions,
    "leave_request_cancelled": LeaveRequestCancelledDimensions,
    "hr_flow_error": HrFlowErrorDimensions,
    "procurement_request_submitted": ProcurementRequestSubmittedDimensions,
    "procurement_request_withdrawn": ProcurementRequestWithdrawnDimensions,
    "approval_task_approved": ApprovalTaskApprovedDimensions,
    "approval_task_rejected": ApprovalTaskRejectedDimensions,
    "procurement_request_completed": ProcurementRequestCompletedDimensions,
    "procurement_flow_error": ProcurementFlowErrorDimensions,
}

PRODUCT_MODULE_KEYS = DEFAULT_MODULE_KEYS


@dataclass(frozen=True, slots=True)
class EventInput:
    event_id: uuid.UUID
    event_name: str
    module_key: str
    actor_user_id: uuid.UUID | None
    organization_unit_id: uuid.UUID | None
    role_snapshot: str
    request_id: str | None
    outcome: str | None
    duration_ms: int | None
    dimensions: Mapping[str, object]


class ProductEventValidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_event_dimensions(
    event_name: str,
    dimensions: Mapping[str, object],
) -> dict[str, object]:
    dimensions_model = EVENT_DIMENSION_MODELS.get(event_name)
    if dimensions_model is None:
        raise ProductEventValidationError("event_name_not_allowed")
    try:
        return dimensions_model.model_validate(dict(dimensions)).model_dump(
            mode="json"
        )
    except (TypeError, ValueError, ValidationError):
        raise ProductEventValidationError("event_dimensions_invalid") from None


class ProductEventEmitter:
    def append(self, db: Session, event: EventInput) -> ProductEvent:
        dimensions = validate_event_dimensions(event.event_name, event.dimensions)
        _validate_event_metadata(event, dimensions)
        canonical = {
            "event_name": event.event_name,
            "module_key": event.module_key,
            "actor_user_id": event.actor_user_id,
            "organization_unit_id": event.organization_unit_id,
            "role_snapshot": event.role_snapshot,
            "request_id": event.request_id,
            "outcome": event.outcome,
            "duration_ms": event.duration_ms,
            "dimensions": dimensions,
        }
        existing = db.scalar(
            select(ProductEvent).where(ProductEvent.event_id == event.event_id)
        )
        if existing is not None:
            stored = {key: getattr(existing, key) for key in canonical}
            if stored != canonical:
                raise ProductEventValidationError("event_id_conflict")
            return existing
        product_event = ProductEvent(
            event_id=event.event_id,
            occurred_at=datetime.now(timezone.utc),
            **canonical,
        )
        db.add(product_event)
        db.flush()
        return product_event


def _validate_event_metadata(
    event: EventInput,
    dimensions: Mapping[str, object],
) -> None:
    if event.module_key not in PRODUCT_MODULE_KEYS:
        raise ProductEventValidationError("event_module_invalid")
    if event.event_name == "workbench_module_opened" and (
        dimensions.get("module_key") != event.module_key
    ):
        raise ProductEventValidationError("event_dimensions_invalid")
    if event.request_id is not None and (
        len(event.request_id) > 120 or _has_control_character(event.request_id)
    ):
        raise ProductEventValidationError("event_request_id_invalid")
    if event.duration_ms is not None and event.duration_ms < 0:
        raise ProductEventValidationError("event_duration_invalid")
    if event.role_snapshot not in {"employee", "hr", "admin", "system"}:
        raise ProductEventValidationError("event_role_invalid")
    if event.outcome is not None and not _is_stable_code(event.outcome):
        raise ProductEventValidationError("event_outcome_invalid")


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _is_stable_code(value: str) -> bool:
    return (
        1 <= len(value) <= 80
        and "a" <= value[0] <= "z"
        and all(
            "a" <= character <= "z"
            or "0" <= character <= "9"
            or character in {"_", ".", "-"}
            for character in value
        )
    )
