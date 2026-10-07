from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from policy_api.observability import log_event
from policy_api.tools.audit import AuditSummary, append_audit_event
from policy_api.tools.enums import (
    ToolAuditEventKind,
    ToolConfirmationStatus,
    ToolInvocationStatus,
)
from policy_api.tools.errors import ToolError
from policy_api.tools.models import ToolConfirmation, ToolInvocation


SAFE_RESULT_FIELDS = frozenset(
    {"status", "request_number", "resource_type", "resource_id", "outcome"}
)


@dataclass(frozen=True, slots=True)
class ToolExecutionResource:
    resource_type: str
    resource_id: UUID
    result_summary: Mapping[str, object]
    replayed: bool = False

    def __post_init__(self) -> None:
        safe_summary = {
            key: value
            for key, value in self.result_summary.items()
            if key in SAFE_RESULT_FIELDS
        }
        object.__setattr__(self, "result_summary", safe_summary)


ToolExecutionCallback = Callable[
    [Session, ToolConfirmation],
    ToolExecutionResource,
]
ExpirationCallback = Callable[[Session, ToolConfirmation], None]
BeforeCommitCallback = Callable[
    [Session, ToolConfirmation, ToolExecutionResource],
    None,
]
FailureMapper = Callable[[Exception], ToolError]


def canonical_arguments_hash(arguments: Mapping[str, object]) -> str:
    canonical = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def create_tool_confirmation(
    db: Session,
    *,
    invocation: ToolInvocation,
    owner_user_id: UUID,
    normalized_arguments: Mapping[str, object],
    preview: Mapping[str, object],
    expires_at: datetime,
    audit_summary: AuditSummary,
) -> ToolConfirmation:
    if expires_at.tzinfo is None:
        raise ToolError("confirmation_expiry_invalid")
    arguments = dict(normalized_arguments)
    arguments_hash = canonical_arguments_hash(arguments)
    confirmation = ToolConfirmation(
        invocation_id=invocation.id,
        owner_user_id=owner_user_id,
        tool_name=invocation.tool_name,
        normalized_arguments=arguments,
        arguments_hash=arguments_hash,
        preview=dict(preview),
        status=ToolConfirmationStatus.PENDING,
        expires_at=expires_at,
    )
    invocation.arguments_hash = arguments_hash
    invocation.status = ToolInvocationStatus.CONFIRMATION_PENDING
    db.add(confirmation)
    db.flush()
    append_audit_event(
        db,
        invocation_id=invocation.id,
        confirmation_id=confirmation.id,
        actor_user_id=owner_user_id,
        event_kind=ToolAuditEventKind.CONFIRMATION_CREATED,
        summary=audit_summary,
    )
    return confirmation


def cancel_tool_confirmation(
    db: Session,
    actor_user_id: UUID,
    confirmation_id: UUID,
) -> ToolConfirmation:
    confirmation = _lock_owned_confirmation(db, actor_user_id, confirmation_id)
    if confirmation is None:
        raise ToolError("confirmation_not_found")
    if confirmation.status == ToolConfirmationStatus.CANCELLED:
        return confirmation
    if confirmation.status == ToolConfirmationStatus.CONSUMED:
        raise ToolError("confirmation_already_used")
    if confirmation.status == ToolConfirmationStatus.EXPIRED:
        raise ToolError("confirmation_expired")
    if confirmation.expires_at <= datetime.now(timezone.utc):
        confirmation.status = ToolConfirmationStatus.EXPIRED
        raise StagedToolExecutionExpired("confirmation_expired")
    confirmation.status = ToolConfirmationStatus.CANCELLED
    confirmation.cancelled_at = datetime.now(timezone.utc)
    return confirmation


def confirm_tool_execution(
    db: Session,
    *,
    actor_user_id: UUID,
    confirmation_id: UUID,
    client_operation_id: UUID,
    normalized_arguments: Mapping[str, object],
    execute: ToolExecutionCallback,
    failure_summary: AuditSummary | None = None,
    on_expired: ExpirationCallback | None = None,
    before_commit: BeforeCommitCallback | None = None,
    failure_mapper: FailureMapper | None = None,
) -> ToolExecutionResource:
    try:
        resource = stage_tool_execution(
            db,
            actor_user_id=actor_user_id,
            confirmation_id=confirmation_id,
            client_operation_id=client_operation_id,
            normalized_arguments=normalized_arguments,
            execute=execute,
            on_expired=on_expired,
            before_commit=before_commit,
        )
        db.commit()
        return resource
    except StagedToolExecutionExpired:
        db.commit()
        raise ToolError("confirmation_expired") from None
    except ToolError:
        db.rollback()
        raise
    except Exception as error:
        db.rollback()
        failure = ToolError("tool_execution_failed")
        if failure_mapper is not None:
            try:
                mapped = failure_mapper(error)
                if isinstance(mapped, ToolError):
                    failure = mapped
            except Exception:
                pass
        failure_stage = failure.metadata.get("failure_stage")
        internal_error_code = failure.metadata.get("internal_error_code")
        if isinstance(failure_stage, str) and isinstance(
            internal_error_code, str
        ):
            log_event(
                "tool_execution_failed",
                stage=failure_stage,
                error_code=internal_error_code,
                outcome="failed",
            )
        confirmation = _lock_owned_confirmation(
            db, actor_user_id, confirmation_id
        )
        invocation_id = (
            confirmation.invocation_id if confirmation is not None else None
        )
        if invocation_id is not None:
            compensate_failed_tool_execution(
                db,
                invocation_id=invocation_id,
                confirmation_id=confirmation_id,
                actor_user_id=actor_user_id,
                summary=failure_summary,
                error_code=failure.code,
            )
        raise failure from None


class StagedToolExecutionExpired(ToolError):
    """Signals that an EXPIRED transition is staged and must be committed."""

    pass


def stage_tool_execution(
    db: Session,
    *,
    actor_user_id: UUID,
    confirmation_id: UUID,
    client_operation_id: UUID,
    normalized_arguments: Mapping[str, object],
    execute: ToolExecutionCallback,
    on_expired: ExpirationCallback | None = None,
    before_commit: BeforeCommitCallback | None = None,
) -> ToolExecutionResource:
    """Lock, validate, execute, and stage confirmation state without commit."""
    confirmation = _lock_owned_confirmation(db, actor_user_id, confirmation_id)
    if confirmation is None:
        raise ToolError("confirmation_not_found")
    current_hash = canonical_arguments_hash(normalized_arguments)
    if current_hash != confirmation.arguments_hash:
        raise ToolError("confirmation_arguments_mismatch")
    if confirmation.status == ToolConfirmationStatus.CONSUMED:
        if (
            confirmation.client_operation_id == client_operation_id
            and confirmation.result_resource_type is not None
            and confirmation.result_resource_id is not None
        ):
            return ToolExecutionResource(
                resource_type=confirmation.result_resource_type,
                resource_id=confirmation.result_resource_id,
                result_summary={"outcome": "replayed"},
                replayed=True,
            )
        raise ToolError("confirmation_already_used")
    if confirmation.status == ToolConfirmationStatus.CANCELLED:
        raise ToolError("confirmation_cancelled")
    if confirmation.status == ToolConfirmationStatus.EXPIRED:
        raise ToolError("confirmation_expired")
    if confirmation.expires_at <= datetime.now(timezone.utc):
        confirmation.status = ToolConfirmationStatus.EXPIRED
        if on_expired is not None:
            on_expired(db, confirmation)
        raise StagedToolExecutionExpired("confirmation_expired")

    _claim_confirmation_operation(
        db,
        confirmation=confirmation,
        actor_user_id=actor_user_id,
        client_operation_id=client_operation_id,
    )

    invocation = db.get(ToolInvocation, confirmation.invocation_id)
    if invocation is None:
        raise ToolError("tool_invocation_not_found")
    invocation.status = ToolInvocationStatus.EXECUTING
    append_audit_event(
        db,
        invocation_id=invocation.id,
        confirmation_id=confirmation.id,
        actor_user_id=actor_user_id,
        event_kind=ToolAuditEventKind.EXECUTION_STARTED,
        summary=AuditSummary.for_execution(
            tool_name=confirmation.tool_name,
            risk_level=invocation.risk_level,
            outcome="started",
            operation_id=client_operation_id,
        ),
    )
    resource = execute(db, confirmation)
    confirmation.status = ToolConfirmationStatus.CONSUMED
    confirmation.client_operation_id = client_operation_id
    confirmation.consumed_at = datetime.now(timezone.utc)
    confirmation.result_resource_type = resource.resource_type
    confirmation.result_resource_id = resource.resource_id
    invocation.status = ToolInvocationStatus.SUCCEEDED
    invocation.result_resource_type = resource.resource_type
    invocation.result_resource_id = resource.resource_id
    append_audit_event(
        db,
        invocation_id=invocation.id,
        confirmation_id=confirmation.id,
        actor_user_id=actor_user_id,
        event_kind=ToolAuditEventKind.EXECUTION_SUCCEEDED,
        summary=AuditSummary.for_execution(
            tool_name=confirmation.tool_name,
            risk_level=invocation.risk_level,
            outcome="succeeded",
            operation_id=client_operation_id,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
        ),
    )
    if before_commit is not None:
        before_commit(db, confirmation, resource)
    return resource


def compensate_failed_tool_execution(
    db: Session,
    *,
    invocation_id: UUID,
    confirmation_id: UUID,
    actor_user_id: UUID,
    summary: AuditSummary | None,
    error_code: str = "tool_execution_failed",
) -> None:
    try:
        invocation = db.get(ToolInvocation, invocation_id)
        if invocation is None:
            db.rollback()
            return
        invocation.status = ToolInvocationStatus.FAILED
        invocation.error_code = error_code
        append_audit_event(
            db,
            invocation_id=invocation_id,
            confirmation_id=confirmation_id,
            actor_user_id=actor_user_id,
            event_kind=ToolAuditEventKind.EXECUTION_FAILED,
            summary=summary
            or AuditSummary.for_execution(
                tool_name=invocation.tool_name,
                risk_level=invocation.risk_level,
                outcome="failed",
            ),
            error_code=error_code,
        )
        db.commit()
    except Exception:
        db.rollback()


def _claim_confirmation_operation(
    db: Session,
    *,
    confirmation: ToolConfirmation,
    actor_user_id: UUID,
    client_operation_id: UUID,
) -> None:
    existing_operation = db.scalar(
        select(ToolConfirmation).where(
            ToolConfirmation.owner_user_id == actor_user_id,
            ToolConfirmation.client_operation_id == client_operation_id,
        )
    )
    if existing_operation is not None:
        if existing_operation.id != confirmation.id:
            raise ToolError("operation_id_conflict")
        return

    begin_nested = getattr(db, "begin_nested", None)
    if not callable(begin_nested):
        confirmation.client_operation_id = client_operation_id
        return
    try:
        with begin_nested():
            confirmation.client_operation_id = client_operation_id
            db.flush()
    except IntegrityError:
        existing_operation = db.scalar(
            select(ToolConfirmation).where(
                ToolConfirmation.owner_user_id == actor_user_id,
                ToolConfirmation.client_operation_id == client_operation_id,
            )
        )
        if existing_operation is not None and existing_operation.id != confirmation.id:
            raise ToolError("operation_id_conflict") from None
        raise


def _lock_owned_confirmation(
    db: Session,
    actor_user_id: UUID,
    confirmation_id: UUID,
) -> ToolConfirmation | None:
    return db.scalar(
        select(ToolConfirmation)
        .where(
            ToolConfirmation.id == confirmation_id,
            ToolConfirmation.owner_user_id == actor_user_id,
        )
        .with_for_update()
    )
