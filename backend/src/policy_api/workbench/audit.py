from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import Enum
import unicodedata
import uuid

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from policy_api.models import Base, UUIDPrimaryKeyMixin, utc_now


AUDIT_SUMMARY_FIELDS = {
    "organization_unit_created": frozenset(
        {"organization_unit_id", "parent_id", "changed_fields"}
    ),
    "organization_unit_updated": frozenset(
        {"organization_unit_id", "parent_id", "changed_fields"}
    ),
    "organization_unit_deactivated": frozenset(
        {"organization_unit_id", "changed_fields"}
    ),
    "employee_assignment_updated": frozenset(
        {
            "employee_id",
            "organization_unit_id",
            "manager_employee_id",
            "changed_fields",
        }
    ),
    "capability_grant_created": frozenset(
        {"grant_id", "capability", "scope_kind", "organization_unit_id"}
    ),
    "capability_grant_revoked": frozenset(
        {"grant_id", "capability", "scope_kind", "organization_unit_id"}
    ),
    "capability_grant_reactivated": frozenset(
        {"grant_id", "capability", "scope_kind", "organization_unit_id"}
    ),
    "procurement_request_submitted": frozenset(
        {
            "procurement_request_id",
            "approval_instance_id",
            "organization_unit_id",
            "item_count_bucket",
            "amount_bucket",
        }
    ),
    "procurement_request_withdrawn": frozenset(
        {"procurement_request_id", "approval_instance_id", "organization_unit_id", "stage"}
    ),
    "approval_task_approved": frozenset(
        {"procurement_request_id", "approval_instance_id", "approval_task_id", "organization_unit_id", "stage", "processing_time_bucket"}
    ),
    "approval_task_rejected": frozenset(
        {"procurement_request_id", "approval_instance_id", "approval_task_id", "organization_unit_id", "stage", "processing_time_bucket"}
    ),
    "procurement_operation_replayed": frozenset(
        {"procurement_request_id", "organization_unit_id", "stage", "code"}
    ),
    "procurement_operation_conflict": frozenset(
        {"procurement_request_id", "organization_unit_id", "stage", "code"}
    ),
    "procurement_permission_denied": frozenset(
        {"procurement_request_id", "organization_unit_id", "stage", "code"}
    ),
    "procurement_confirmation_shown": frozenset(
        {"confirmation_id", "stage", "tool_name"}
    ),
    "procurement_confirmation_confirmed": frozenset(
        {"procurement_request_id", "organization_unit_id", "confirmation_id", "stage", "tool_name"}
    ),
    "procurement_confirmation_cancelled": frozenset(
        {"confirmation_id", "stage", "tool_name"}
    ),
    "procurement_confirmation_expired": frozenset(
        {"confirmation_id", "stage", "tool_name"}
    ),
}

AUDIT_TARGET_TYPES = {
    "organization_unit_created": "organization_unit",
    "organization_unit_updated": "organization_unit",
    "organization_unit_deactivated": "organization_unit",
    "employee_assignment_updated": "employee_assignment",
    "capability_grant_created": "capability_grant",
    "capability_grant_revoked": "capability_grant",
    "capability_grant_reactivated": "capability_grant",
    "procurement_request_submitted": "procurement_request",
    **{
        name: "procurement_request"
        for name in (
            "procurement_request_withdrawn", "approval_task_approved",
            "approval_task_rejected", "procurement_operation_replayed",
            "procurement_operation_conflict", "procurement_permission_denied",
            "procurement_confirmation_shown", "procurement_confirmation_confirmed",
            "procurement_confirmation_cancelled", "procurement_confirmation_expired",
        )
    },
}

AUDIT_EXPECTED_OUTCOME = {
    **{
        event_name: "succeeded"
        for event_name in (
            "organization_unit_created",
            "organization_unit_updated",
            "organization_unit_deactivated",
            "employee_assignment_updated",
            "capability_grant_created",
            "capability_grant_revoked",
            "capability_grant_reactivated",
            "procurement_request_submitted",
            "procurement_request_withdrawn",
            "approval_task_approved",
            "approval_task_rejected",
        )
    },
    "procurement_operation_replayed": "replayed",
    "procurement_operation_conflict": "conflict",
    "procurement_permission_denied": "denied",
    "procurement_confirmation_shown": "shown",
    "procurement_confirmation_confirmed": "confirmed",
    "procurement_confirmation_cancelled": "cancelled",
    "procurement_confirmation_expired": "expired",
}
AUDIT_TARGET_ID_REQUIREMENT = {
    **{event_name: "required" for event_name in AUDIT_EXPECTED_OUTCOME},
    "procurement_operation_replayed": "optional",
    "procurement_operation_conflict": "optional",
    "procurement_permission_denied": "optional",
    "procurement_confirmation_shown": "empty",
    "procurement_confirmation_cancelled": "empty",
    "procurement_confirmation_expired": "empty",
}
AUDIT_SCOPE_KINDS = frozenset({"global", "unit_subtree"})
AUDIT_CHANGED_FIELDS = {
    "organization_unit_created": frozenset(
        {"code", "name", "parent_id", "is_active"}
    ),
    "organization_unit_updated": frozenset(
        {"code", "name", "parent_id", "is_active"}
    ),
    "organization_unit_deactivated": frozenset({"is_active"}),
    "employee_assignment_updated": frozenset(
        {"organization_unit_id", "manager_employee_id"}
    ),
}
SENSITIVE_KEY_PARTS = frozenset(
    {"reason", "message", "password", "token", "secret"}
)
UUID_SUMMARY_FIELDS = frozenset(
    {
        "organization_unit_id",
        "parent_id",
        "employee_id",
        "manager_employee_id",
        "grant_id",
        "procurement_request_id",
        "approval_instance_id",
        "approval_task_id",
        "confirmation_id",
    }
)
AUDIT_ITEM_COUNT_BUCKETS = frozenset({"1", "2_5", "6_10", "11_plus"})
AUDIT_AMOUNT_BUCKETS = frozenset(
    {"0_999", "1000_9999", "10000_99999", "100000_plus"}
)
AUDIT_STAGES = frozenset(
    {"submission", "withdrawal", "department_review", "procurement_review", "confirmation"}
)
AUDIT_PROCESSING_TIME_BUCKETS = frozenset(
    {
        "lt_1s", "1s_3s", "3s_10s", "gte_10s", "same_day", "later",
        "10s_24h", "24h_48h", "gte_48h",
    }
)
AUDIT_TOOL_NAMES = frozenset(
    {"procurement.submit_request", "procurement.withdraw_request", "approval.approve_task", "approval.reject_task"}
)
AUDIT_CODES = frozenset(
    {
        "exact_replay",
        "operation_id_conflict",
        "procurement_request_conflict",
        "procurement_request_state_conflict",
        "approval_operation_id_conflict",
        "approval_instance_state_conflict",
        "approval_task_state_conflict",
        "procurement_profile_required",
        "procurement_manager_capability_required",
        "procurement_manager_unavailable",
        "approval_capability_required",
        "approval_scope_denied",
        "approval_task_not_assigned",
        "approval_assignment_mismatch",
        "procurement_request_not_found",
        "approval_task_not_found",
    }
)
NULLABLE_UUID_SUMMARY_FIELDS = frozenset(
    {"organization_unit_id", "parent_id", "manager_employee_id", "procurement_request_id", "approval_instance_id", "approval_task_id"}
)


class SecurityAuditValidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SecurityAuditEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "security_audit_events"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id",
            "operation_id",
            name="uq_security_audit_actor_operation",
        ),
    )

    event_name: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(80), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(120))
    summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


def append_security_audit(
    db: Session,
    *,
    event_name: str,
    actor_user_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID | None,
    operation_id: uuid.UUID,
    outcome: str,
    request_id: str | None,
    summary: Mapping[str, object],
) -> SecurityAuditEvent:
    normalized_target_id = _normalize_optional_uuid(
        target_id,
        code="security_audit_target_invalid",
    )
    normalized_summary = _normalize_summary(event_name, summary)
    _validate_envelope(
        event_name=event_name,
        target_type=target_type,
        target_id=normalized_target_id,
        outcome=outcome,
        request_id=request_id,
    )
    if not isinstance(actor_user_id, uuid.UUID) or not isinstance(
        operation_id, uuid.UUID
    ):
        raise SecurityAuditValidationError("security_audit_identity_invalid")

    existing = db.scalar(
        select(SecurityAuditEvent).where(
            SecurityAuditEvent.actor_user_id == actor_user_id,
            SecurityAuditEvent.operation_id == operation_id,
        ).execution_options(populate_existing=True)
    )
    expected_payload = {
        "event_name": event_name,
        "actor_user_id": actor_user_id,
        "target_type": target_type,
        "target_id": normalized_target_id,
        "operation_id": operation_id,
        "outcome": outcome,
        "request_id": request_id,
        "summary": normalized_summary,
    }
    if existing is not None:
        if _persistent_payload(existing) != expected_payload:
            raise SecurityAuditValidationError(
                "security_audit_operation_conflict"
            )
        return existing

    event = SecurityAuditEvent(**expected_payload)
    db.add(event)
    db.flush()
    return event


def _validate_envelope(
    *,
    event_name: str,
    target_type: str,
    target_id: uuid.UUID | None,
    outcome: str,
    request_id: str | None,
) -> None:
    if event_name not in AUDIT_SUMMARY_FIELDS:
        raise SecurityAuditValidationError("security_audit_event_invalid")
    if target_type != AUDIT_TARGET_TYPES[event_name]:
        raise SecurityAuditValidationError("security_audit_target_invalid")
    if outcome != AUDIT_EXPECTED_OUTCOME[event_name]:
        raise SecurityAuditValidationError("security_audit_outcome_invalid")
    target_requirement = AUDIT_TARGET_ID_REQUIREMENT[event_name]
    if (
        (target_requirement == "required" and target_id is None)
        or (target_requirement == "empty" and target_id is not None)
    ):
        raise SecurityAuditValidationError("security_audit_target_invalid")
    if request_id is not None and (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > 120
        or _has_control_character(request_id)
    ):
        raise SecurityAuditValidationError("security_audit_request_id_invalid")


def _normalize_summary(
    event_name: str,
    summary: Mapping[str, object],
) -> dict[str, object]:
    if event_name not in AUDIT_SUMMARY_FIELDS:
        raise SecurityAuditValidationError("security_audit_event_invalid")
    if not isinstance(summary, Mapping):
        raise SecurityAuditValidationError("security_audit_summary_invalid")
    _reject_sensitive_or_controlled_content(summary)
    if frozenset(summary) != AUDIT_SUMMARY_FIELDS[event_name]:
        raise SecurityAuditValidationError("security_audit_summary_invalid")

    normalized: dict[str, object] = {}
    for key, value in summary.items():
        if key in UUID_SUMMARY_FIELDS:
            if value is None and key in NULLABLE_UUID_SUMMARY_FIELDS:
                normalized[key] = None
            else:
                normalized[key] = str(
                    _normalize_uuid(value, code="security_audit_summary_invalid")
                )
        elif key == "capability":
            normalized[key] = _normalize_catalog_value(
                value,
                _audit_capabilities(),
                code="security_audit_summary_invalid",
            )
        elif key == "scope_kind":
            normalized[key] = _normalize_catalog_value(
                value,
                AUDIT_SCOPE_KINDS,
                code="security_audit_summary_invalid",
            )
        elif key == "changed_fields":
            normalized[key] = _normalize_changed_fields(event_name, value)
        elif key == "item_count_bucket":
            normalized[key] = _normalize_catalog_value(
                value,
                AUDIT_ITEM_COUNT_BUCKETS,
                code="security_audit_summary_invalid",
            )
        elif key == "amount_bucket":
            normalized[key] = _normalize_catalog_value(
                value,
                AUDIT_AMOUNT_BUCKETS,
                code="security_audit_summary_invalid",
            )
        elif key == "stage":
            normalized[key] = _normalize_catalog_value(value, AUDIT_STAGES, code="security_audit_summary_invalid")
        elif key == "processing_time_bucket":
            normalized[key] = _normalize_catalog_value(value, AUDIT_PROCESSING_TIME_BUCKETS, code="security_audit_summary_invalid")
        elif key == "tool_name":
            normalized[key] = _normalize_catalog_value(value, AUDIT_TOOL_NAMES, code="security_audit_summary_invalid")
        elif key == "code":
            normalized[key] = _normalize_catalog_value(value, AUDIT_CODES, code="security_audit_summary_invalid")
        else:
            raise SecurityAuditValidationError("security_audit_summary_invalid")
    return normalized


def _audit_capabilities() -> frozenset[str]:
    # Imported lazily because capabilities owns organization services that
    # append these audit events.
    from policy_api.workbench.capabilities import Capability

    return frozenset(capability.value for capability in Capability)


def _normalize_changed_fields(event_name: str, value: object) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not value
        or any(not isinstance(field, str) for field in value)
    ):
        raise SecurityAuditValidationError("security_audit_summary_invalid")
    fields = list(value)
    if not set(fields).issubset(
        AUDIT_CHANGED_FIELDS.get(event_name, frozenset())
    ):
        raise SecurityAuditValidationError("security_audit_summary_invalid")
    return sorted(set(fields))


def _normalize_catalog_value(
    value: object,
    allowed_values: frozenset[str],
    *,
    code: str,
) -> str:
    raw_value = value.value if isinstance(value, Enum) else value
    if not isinstance(raw_value, str) or raw_value not in allowed_values:
        raise SecurityAuditValidationError(code)
    return raw_value


def _normalize_optional_uuid(
    value: object,
    *,
    code: str,
) -> uuid.UUID | None:
    return None if value is None else _normalize_uuid(value, code=code)


def _normalize_uuid(value: object, *, code: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            pass
    raise SecurityAuditValidationError(code)


def _reject_sensitive_or_controlled_content(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if (
                not isinstance(key, str)
                or _has_control_character(key)
                or any(
                    part in key.casefold().replace("-", "_").split("_")
                    for part in SENSITIVE_KEY_PARTS
                )
            ):
                raise SecurityAuditValidationError(
                    "security_audit_summary_invalid"
                )
            _reject_sensitive_or_controlled_content(nested_value)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            _reject_sensitive_or_controlled_content(item)
    elif isinstance(value, str) and _has_control_character(value):
        raise SecurityAuditValidationError("security_audit_summary_invalid")


def _has_control_character(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C")
        for character in value
    )


def _persistent_payload(event: SecurityAuditEvent) -> dict[str, object]:
    return {
        "event_name": event.event_name,
        "actor_user_id": event.actor_user_id,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "operation_id": event.operation_id,
        "outcome": event.outcome,
        "request_id": event.request_id,
        "summary": event.summary,
    }
