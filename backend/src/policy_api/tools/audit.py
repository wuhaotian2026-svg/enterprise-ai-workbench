from __future__ import annotations

from dataclasses import dataclass
import hashlib
from uuid import UUID

from sqlalchemy.orm import Session

from policy_api.tools.enums import ToolAuditEventKind
from policy_api.tools.models import ToolAuditEvent


def hash_sensitive_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditSummary:
    tool_name: str
    outcome: str
    risk_level: str | None = None
    operation_id: UUID | None = None
    duration_ms: int | None = None
    resource_type: str | None = None
    resource_id: UUID | None = None
    reason_hash: str | None = None

    @classmethod
    def for_execution(
        cls,
        *,
        tool_name: str,
        outcome: str,
        risk_level: str | None = None,
        operation_id: UUID | None = None,
        duration_ms: int | None = None,
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        reason: str | None = None,
    ) -> AuditSummary:
        return cls(
            tool_name=tool_name,
            outcome=outcome,
            risk_level=risk_level,
            operation_id=operation_id,
            duration_ms=duration_ms,
            resource_type=resource_type,
            resource_id=resource_id,
            reason_hash=hash_sensitive_text(reason) if reason is not None else None,
        )

    def payload(self) -> dict[str, object]:
        values: tuple[tuple[str, object | None], ...] = (
            ("tool_name", self.tool_name),
            ("risk_level", self.risk_level),
            ("outcome", self.outcome),
            (
                "operation_id",
                str(self.operation_id) if self.operation_id is not None else None,
            ),
            ("duration_ms", self.duration_ms),
            ("resource_type", self.resource_type),
            (
                "resource_id",
                str(self.resource_id) if self.resource_id is not None else None,
            ),
            ("reason_hash", self.reason_hash),
        )
        return {key: value for key, value in values if value is not None}


def append_audit_event(
    db: Session,
    *,
    invocation_id: UUID,
    actor_user_id: UUID,
    event_kind: ToolAuditEventKind,
    summary: AuditSummary,
    confirmation_id: UUID | None = None,
    error_code: str | None = None,
) -> ToolAuditEvent:
    event = ToolAuditEvent(
        invocation_id=invocation_id,
        confirmation_id=confirmation_id,
        actor_user_id=actor_user_id,
        event_kind=event_kind,
        summary=summary.payload(),
        error_code=error_code,
    )
    db.add(event)
    db.flush()
    return event
