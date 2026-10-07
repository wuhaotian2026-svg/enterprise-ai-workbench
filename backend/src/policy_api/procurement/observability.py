from __future__ import annotations

from datetime import datetime
import json
import uuid

from sqlalchemy.orm import Session

from policy_api.approvals.enums import ApprovalCommandKind, ApprovalInstanceStatus
from policy_api.approvals.models import ApprovalTask
from policy_api.approvals.service import ApprovalTransition
from policy_api.procurement.repository import ProcurementRepository
from policy_api.workbench.audit import append_security_audit
from policy_api.workbench.events import EventInput, ProductEventEmitter


EVENT_NAMESPACE = uuid.UUID("63fbe1c7-c7ea-5b25-a5a4-487778d99725")
ATTEMPT_NAMESPACE = uuid.UUID("f87c1e7e-b08d-5a36-a781-6ad3b35ea247")
CONFIRMATION_NAMESPACE = uuid.UUID("9db53551-c54e-5ca3-a1f4-97a94e4aaeb1")
PROCUREMENT_CONFLICT_CODES = frozenset({
    "operation_id_conflict",
    "procurement_request_conflict",
    "procurement_request_state_conflict",
    "approval_operation_id_conflict",
    "approval_instance_state_conflict",
    "approval_task_state_conflict",
})
PROCUREMENT_DENIAL_CODES = frozenset({
    "procurement_profile_required",
    "procurement_manager_capability_required",
    "procurement_manager_unavailable",
    "approval_capability_required",
    "approval_scope_denied",
    "approval_task_not_assigned",
    "approval_assignment_mismatch",
    "procurement_request_not_found",
    "approval_task_not_found",
})


def _derived_id(namespace: uuid.UUID, values: list[str]) -> uuid.UUID:
    return uuid.uuid5(namespace, json.dumps(values, separators=(",", ":")))


def attempt_audit_operation_id(
    actor_id: uuid.UUID, client_operation_id: uuid.UUID, server_attempt_id: str,
    command_kind: str, outcome: str,
) -> uuid.UUID:
    if outcome not in {"replayed", "conflict", "denied"}:
        raise ValueError("procurement_attempt_outcome_invalid")
    return _derived_id(ATTEMPT_NAMESPACE, [
        "procurement-attempt-v1", str(actor_id), str(client_operation_id),
        server_attempt_id, command_kind, outcome,
    ])


def confirmation_audit_operation_id(
    actor_id: uuid.UUID, confirmation_id: uuid.UUID, event_name: str,
) -> uuid.UUID:
    if event_name not in {
        "procurement_confirmation_shown", "procurement_confirmation_confirmed",
        "procurement_confirmation_cancelled", "procurement_confirmation_expired",
    }:
        raise ValueError("procurement_confirmation_event_invalid")
    return _derived_id(CONFIRMATION_NAMESPACE, [
        "procurement-confirmation-v1", str(actor_id), str(confirmation_id), event_name,
    ])


def attempt_outcome_for_code(code: str) -> str | None:
    if code in PROCUREMENT_CONFLICT_CODES:
        return "conflict"
    if code in PROCUREMENT_DENIAL_CODES:
        return "denied"
    return None


def processing_time_bucket(start: datetime | None, end: datetime) -> str:
    if start is None:
        return "later"
    seconds = max(0.0, (end - start).total_seconds())
    if seconds < 1:
        return "lt_1s"
    if seconds < 3:
        return "1s_3s"
    if seconds < 10:
        return "3s_10s"
    if seconds < 86_400:
        return "10s_24h"
    if seconds < 172_800:
        return "24h_48h"
    return "gte_48h"


def lifecycle_event_id(actor_id: uuid.UUID, operation_id: uuid.UUID, event_name: str) -> uuid.UUID:
    return _derived_id(EVENT_NAMESPACE, [
        "procurement-lifecycle-v1", str(actor_id), str(operation_id), event_name,
    ])


class ProcurementObservability:
    def __init__(
        self,
        repository: ProcurementRepository,
        emitter: ProductEventEmitter | None = None,
    ) -> None:
        self._repository = repository
        self._emitter = emitter or ProductEventEmitter()

    def stage_attempt(
        self, db: Session, *, actor_user_id: uuid.UUID, actor_role: str,
        client_operation_id: uuid.UUID, server_attempt_id: str,
        command_kind: str, outcome: str | None, code: str,
        stage: str | None, request_id: uuid.UUID | None = None,
        organization_unit_id: uuid.UUID | None = None,
        approval_task_id: uuid.UUID | None = None,
    ) -> None:
        classified_outcome = attempt_outcome_for_code(code)
        if outcome is None:
            outcome = classified_outcome
        elif outcome != "replayed" and outcome != classified_outcome:
            raise ValueError("procurement_attempt_outcome_invalid")
        if outcome is None:
            return
        if approval_task_id is not None:
            task = db.get(ApprovalTask, approval_task_id)
            request = (
                self._repository.get_request_by_instance(db, task.instance_id)
                if task is not None else None
            )
            stage = {
                "department_manager_review": "department_review",
                "procurement_review": "procurement_review",
            }.get(task.step_key if task is not None else "", stage)
            if request is not None:
                request_id = request.id
                organization_unit_id = request.organization_unit_id
        elif request_id is not None and organization_unit_id is None:
            request = self._repository.get_request(db, request_id)
            if request is not None:
                organization_unit_id = request.organization_unit_id
        if stage is None:
            stage = "department_review"
        event_name = {
            "replayed": "procurement_operation_replayed",
            "conflict": "procurement_operation_conflict",
            "denied": "procurement_permission_denied",
        }[outcome]
        derived_id = attempt_audit_operation_id(
            actor_user_id, client_operation_id, server_attempt_id,
            command_kind, outcome,
        )
        append_security_audit(
            db, event_name=event_name, actor_user_id=actor_user_id,
            target_type="procurement_request", target_id=request_id,
            operation_id=derived_id, outcome=outcome,
            request_id=server_attempt_id,
            summary={
                "procurement_request_id": request_id,
                "organization_unit_id": organization_unit_id,
                "stage": stage, "code": code,
            },
        )
        if outcome != "replayed":
            self._emitter.append(db, EventInput(
                event_id=lifecycle_event_id(actor_user_id, derived_id, "procurement_flow_error"),
                event_name="procurement_flow_error", module_key="procurement",
                actor_user_id=actor_user_id,
                organization_unit_id=organization_unit_id,
                role_snapshot=actor_role, request_id=server_attempt_id,
                outcome=outcome, duration_ms=None,
                dimensions={"stage": stage, "error_code": code, "outcome": outcome},
            ))

    def stage_replayed_transition(
        self, db: Session, *, actor_user_id: uuid.UUID, actor_role: str,
        transition: ApprovalTransition, command_kind: ApprovalCommandKind,
        server_attempt_id: str, stage: str | None = None,
    ) -> None:
        request = self._repository.get_request_by_instance(db, transition.instance.id)
        if stage is None:
            task = next((item for item in transition.tasks if item.id == transition.operation.task_id), None)
            stage = {
                "department_manager_review": "department_review",
                "procurement_review": "procurement_review",
            }.get(task.step_key if task is not None else "", "submission")
        self.stage_attempt(
            db, actor_user_id=actor_user_id, actor_role=actor_role,
            client_operation_id=transition.operation.client_operation_id,
            server_attempt_id=server_attempt_id,
            command_kind=command_kind.value, outcome="replayed",
            code="exact_replay", stage=stage,
            request_id=request.id if request is not None else None,
            organization_unit_id=request.organization_unit_id if request is not None else None,
        )

    def stage_confirmation(
        self, db: Session, *, actor_user_id: uuid.UUID, actor_role: str,
        confirmation_id: uuid.UUID, event_name: str, tool_name: str,
        request_id: str | None, procurement_request_id: uuid.UUID | None = None,
    ) -> None:
        outcome = event_name.removeprefix("procurement_confirmation_")
        request = (
            self._repository.get_request(db, procurement_request_id)
            if procurement_request_id is not None else None
        )
        summary: dict[str, object] = {
            "confirmation_id": confirmation_id, "stage": "confirmation",
            "tool_name": tool_name,
        }
        if event_name == "procurement_confirmation_confirmed":
            summary.update({
                "procurement_request_id": request.id if request is not None else None,
                "organization_unit_id": request.organization_unit_id if request is not None else None,
            })
        append_security_audit(
            db, event_name=event_name, actor_user_id=actor_user_id,
            target_type="procurement_request",
            target_id=request.id if request is not None else None,
            operation_id=confirmation_audit_operation_id(actor_user_id, confirmation_id, event_name),
            outcome=outcome, request_id=request_id, summary=summary,
        )
        product_name = {
            "procurement_confirmation_shown": "confirmation_shown",
            "procurement_confirmation_confirmed": "confirmation_confirmed",
            "procurement_confirmation_cancelled": "confirmation_cancelled",
            "procurement_confirmation_expired": "confirmation_expired",
        }[event_name]
        dimensions = {"tool_name": tool_name}
        if product_name == "confirmation_shown":
            dimensions["risk_level"] = "write"
        self._emitter.append(db, EventInput(
            event_id=lifecycle_event_id(actor_user_id, confirmation_id, product_name),
            event_name=product_name, module_key="procurement",
            actor_user_id=actor_user_id,
            organization_unit_id=request.organization_unit_id if request is not None else None,
            role_snapshot=actor_role, request_id=request_id,
            outcome=outcome, duration_ms=None, dimensions=dimensions,
        ))

    def stage_transition(
        self,
        db: Session,
        *,
        actor_user_id: uuid.UUID,
        actor_role: str,
        transition: ApprovalTransition,
        command_kind: ApprovalCommandKind,
        occurred_at: datetime,
        request_id: str | None,
        channel: str,
    ) -> None:
        if transition.replayed:
            return
        request = self._repository.get_request_by_instance(db, transition.instance.id)
        if request is None:
            return
        task = transition.decision and next(
            (item for item in transition.tasks if item.id == transition.decision.task_id), None
        )
        if command_kind is ApprovalCommandKind.CANCEL:
            event_name = "procurement_request_withdrawn"
            stage = "withdrawal"
            audit_summary = {
                "procurement_request_id": request.id,
                "approval_instance_id": transition.instance.id,
                "organization_unit_id": request.organization_unit_id,
                "stage": stage,
            }
            dimensions = {
                "stage": stage,
                "processing_time_bucket": processing_time_bucket(
                    transition.instance.submitted_at, occurred_at
                ),
            }
        else:
            if task is None:
                return
            stage = {
                "department_manager_review": "department_review",
                "procurement_review": "procurement_review",
            }.get(task.step_key)
            if stage is None:
                return
            event_name = (
                "approval_task_approved"
                if command_kind is ApprovalCommandKind.APPROVE
                else "approval_task_rejected"
            )
            bucket = processing_time_bucket(task.activated_at, occurred_at)
            audit_summary = {
                "procurement_request_id": request.id,
                "approval_instance_id": transition.instance.id,
                "approval_task_id": task.id,
                "organization_unit_id": request.organization_unit_id,
                "stage": stage,
                "processing_time_bucket": bucket,
            }
            dimensions = {"stage": stage, "processing_time_bucket": bucket}
        operation_id = transition.operation.client_operation_id
        append_security_audit(
            db,
            event_name=event_name,
            actor_user_id=actor_user_id,
            target_type="procurement_request",
            target_id=request.id,
            operation_id=operation_id,
            outcome="succeeded",
            request_id=request_id,
            summary=audit_summary,
        )
        self._append_event(
            db, event_name=event_name, actor_user_id=actor_user_id,
            actor_role=actor_role, organization_unit_id=request.organization_unit_id,
            operation_id=operation_id, request_id=request_id,
            dimensions=dimensions,
            duration_ms=self._duration_ms(task.activated_at if task else transition.instance.submitted_at, occurred_at),
        )
        if transition.instance.status in {ApprovalInstanceStatus.APPROVED, ApprovalInstanceStatus.REJECTED}:
            outcome = transition.instance.status.value
            self._append_event(
                db, event_name="procurement_request_completed",
                actor_user_id=actor_user_id, actor_role=actor_role,
                organization_unit_id=request.organization_unit_id,
                operation_id=operation_id, request_id=request_id,
                dimensions={
                    "stage": "completion", "outcome": outcome,
                    "processing_time_bucket": processing_time_bucket(transition.instance.submitted_at, occurred_at),
                },
                duration_ms=self._duration_ms(transition.instance.submitted_at, occurred_at),
            )

    def _append_event(self, db: Session, *, event_name: str, actor_user_id: uuid.UUID,
                      actor_role: str, organization_unit_id: uuid.UUID,
                      operation_id: uuid.UUID, request_id: str | None,
                      dimensions: dict[str, object], duration_ms: int | None) -> None:
        self._emitter.append(db, EventInput(
            event_id=lifecycle_event_id(actor_user_id, operation_id, event_name),
            event_name=event_name, module_key="procurement",
            actor_user_id=actor_user_id, organization_unit_id=organization_unit_id,
            role_snapshot=actor_role, request_id=request_id, outcome="succeeded",
            duration_ms=duration_ms, dimensions=dimensions,
        ))

    @staticmethod
    def _duration_ms(start: datetime | None, end: datetime) -> int | None:
        return None if start is None else max(0, int((end - start).total_seconds() * 1000))
