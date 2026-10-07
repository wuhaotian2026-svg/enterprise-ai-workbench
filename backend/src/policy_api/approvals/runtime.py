"""Domain-neutral approval application runtime."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Protocol
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from policy_api.approvals.authorization import ApprovalAuthorizationPort
from policy_api.approvals.enums import ApprovalCommandKind, ApprovalTaskStatus, AssignmentKind
from policy_api.approvals.models import (
    ApprovalDecision,
    ApprovalInstance,
    ApprovalTask,
)
from policy_api.approvals.schemas import (
    ApprovalTaskDetail,
    ApprovalTaskSummary,
)
from policy_api.approvals.service import ApprovalEngine, ApprovalTransition
from policy_api.approvals.subject_adapter import (
    ApprovalError,
    SubjectAdapterRegistry,
)
from policy_api.models import User
from policy_api.tools.errors import ToolError


class ApprovalAccessPort(Protocol):
    """Actor visibility and lock-time decision authorization boundary."""

    def can_view(
        self,
        db: Session,
        *,
        actor: User,
        instance: ApprovalInstance,
        task: ApprovalTask,
        decision: ApprovalDecision | None,
    ) -> bool: ...

    def authorization(self, db: Session) -> ApprovalAuthorizationPort: ...


class ApprovalRuntime:
    def __init__(
        self,
        *,
        registry: SubjectAdapterRegistry,
        access: ApprovalAccessPort,
        engine: ApprovalEngine | None = None,
        now_factory: Callable[[], datetime] | None = None,
        transition_observer: Callable[..., None] | None = None,
        replay_observer: Callable[..., None] | None = None,
        failure_observer: Callable[..., None] | None = None,
    ) -> None:
        self._registry = registry
        self._access = access
        self._engine = engine if engine is not None else ApprovalEngine()
        self._now_factory = now_factory or (
            lambda: datetime.now(timezone.utc)
        )
        self._transition_observer = transition_observer
        self._replay_observer = replay_observer
        self._failure_observer = failure_observer

    def list_actor_tasks(
        self,
        db: Session,
        *,
        actor: User,
        status: ApprovalTaskStatus | None = ApprovalTaskStatus.PENDING,
    ) -> Sequence[ApprovalTaskSummary]:
        actor_id = self._actor_id(actor)
        statement = (
            select(ApprovalTask, ApprovalInstance, ApprovalDecision)
            .join(
                ApprovalInstance,
                ApprovalInstance.id == ApprovalTask.instance_id,
            )
            .outerjoin(ApprovalDecision, ApprovalDecision.task_id == ApprovalTask.id)
            .where(
                or_(
                    ApprovalTask.assigned_user_id == actor_id,
                    ApprovalDecision.actor_user_id == actor_id,
                    ApprovalTask.assignment_kind == AssignmentKind.CAPABILITY,
                )
            )
            .order_by(
                ApprovalTask.activated_at.desc().nullslast(),
                ApprovalTask.id,
            )
        )
        if status is not None:
            statement = statement.where(ApprovalTask.status == status)
        visible: list[ApprovalTaskSummary] = []
        for task, instance, decision in db.execute(statement):
            if self._access.can_view(
                db,
                actor=actor,
                instance=instance,
                task=task,
                decision=decision,
            ):
                visible.append(self._task_summary(db, instance, task))
        return tuple(visible)

    def get_actor_task(
        self,
        db: Session,
        *,
        actor: User,
        task_id: uuid.UUID,
    ) -> ApprovalTaskDetail:
        self._actor_id(actor)
        row = db.execute(
            select(ApprovalTask, ApprovalInstance, ApprovalDecision)
            .join(
                ApprovalInstance,
                ApprovalInstance.id == ApprovalTask.instance_id,
            )
            .outerjoin(ApprovalDecision, ApprovalDecision.task_id == ApprovalTask.id)
            .where(ApprovalTask.id == task_id)
        ).one_or_none()
        if row is None:
            raise ApprovalError("approval_task_not_found")
        task, instance, decision = row
        if not self._access.can_view(
            db,
            actor=actor,
            instance=instance,
            task=task,
            decision=decision,
        ):
            raise ApprovalError("approval_task_not_found")
        return ApprovalTaskDetail(
            task=self._task_summary(db, instance, task),
            subject=self._registry.detail(instance.subject_type, db, instance),
        )

    def approve_task(
        self,
        db: Session,
        *,
        actor: User,
        task_id: uuid.UUID,
        client_operation_id: uuid.UUID,
        comment: str | None,
        request_id: str | None = None,
        channel: str = "manual",
        commit: bool = True,
    ) -> ApprovalTransition:
        try:
            occurred_at = self._now_factory()
            transition = self._engine.approve_task(
                db,
                task_id=task_id,
                actor=actor,
                client_operation_id=client_operation_id,
                comment=comment,
                authorize=self._access.authorization(db),
                now=occurred_at,
            )
            if self._transition_observer is not None and not transition.replayed:
                self._transition_observer(
                    db, actor_user_id=actor.id, actor_role=actor.role.value,
                    transition=transition, command_kind=ApprovalCommandKind.APPROVE,
                    occurred_at=occurred_at, request_id=request_id, channel=channel,
                )
            elif self._replay_observer is not None and request_id is not None:
                self._replay_observer(
                    db, actor_user_id=actor.id, actor_role=actor.role.value,
                    transition=transition, command_kind=ApprovalCommandKind.APPROVE,
                    server_attempt_id=request_id,
                )
            if commit:
                db.commit()
            return transition
        except Exception as exc:
            if commit:
                db.rollback()
                self._record_failure(
                    db, exc, actor, task_id, client_operation_id,
                    request_id, "approval.approve",
                )
            raise

    def reject_task(
        self,
        db: Session,
        *,
        actor: User,
        task_id: uuid.UUID,
        client_operation_id: uuid.UUID,
        reason: str,
        request_id: str | None = None,
        channel: str = "manual",
        commit: bool = True,
    ) -> ApprovalTransition:
        try:
            occurred_at = self._now_factory()
            transition = self._engine.reject_task(
                db,
                task_id=task_id,
                actor=actor,
                client_operation_id=client_operation_id,
                comment=reason,
                authorize=self._access.authorization(db),
                now=occurred_at,
            )
            if self._transition_observer is not None and not transition.replayed:
                self._transition_observer(
                    db, actor_user_id=actor.id, actor_role=actor.role.value,
                    transition=transition, command_kind=ApprovalCommandKind.REJECT,
                    occurred_at=occurred_at, request_id=request_id, channel=channel,
                )
            elif self._replay_observer is not None and request_id is not None:
                self._replay_observer(
                    db, actor_user_id=actor.id, actor_role=actor.role.value,
                    transition=transition, command_kind=ApprovalCommandKind.REJECT,
                    server_attempt_id=request_id,
                )
            if commit:
                db.commit()
            return transition
        except Exception as exc:
            if commit:
                db.rollback()
                self._record_failure(
                    db, exc, actor, task_id, client_operation_id,
                    request_id, "approval.reject",
                )
            raise

    def _record_failure(self, db: Session, exc: Exception, actor: User,
                        task_id: uuid.UUID, operation_id: uuid.UUID,
                        request_id: str | None, command_kind: str) -> None:
        if self._failure_observer is None or request_id is None or not isinstance(exc, ToolError):
            return
        if exc.code in {
            "approval_operation_id_conflict",
            "approval_instance_state_conflict",
            "approval_task_state_conflict",
        }:
            outcome = "conflict"
        elif exc.code in {
            "approval_capability_required",
            "approval_scope_denied",
            "approval_task_not_assigned",
            "approval_assignment_mismatch",
            "approval_task_not_found",
        }:
            outcome = "denied"
        else:
            return
        try:
            self._failure_observer(
                db, actor_user_id=actor.id, actor_role=actor.role.value,
                client_operation_id=operation_id, server_attempt_id=request_id,
                command_kind=command_kind, outcome=outcome, code=exc.code,
                stage=None, approval_task_id=task_id,
            )
            db.commit()
        except Exception as evidence_error:
            db.rollback()
            exc.add_note(
                "approval/transition evidence failed: "
                f"{type(evidence_error).__name__}"
            )

    def _task_summary(
        self,
        db: Session,
        instance: ApprovalInstance,
        task: ApprovalTask,
    ) -> ApprovalTaskSummary:
        return ApprovalTaskSummary(
            task_id=task.id,
            instance_id=instance.id,
            process_key=instance.process_key,
            subject_type=instance.subject_type,
            step_key=task.step_key,
            step_label=task.step_label,
            status=task.status.value,
            submitted_at=instance.submitted_at,
            activated_at=task.activated_at,
            completed_at=task.completed_at,
            subject=self._registry.summary(instance.subject_type, db, instance),
        )

    @staticmethod
    def _actor_id(actor: User) -> uuid.UUID:
        actor_id = getattr(actor, "id", None)
        if not isinstance(actor_id, uuid.UUID):
            raise ApprovalError("approval_task_not_found")
        return actor_id


__all__ = ["ApprovalAccessPort", "ApprovalRuntime"]
