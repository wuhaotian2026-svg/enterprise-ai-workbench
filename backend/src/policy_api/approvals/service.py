"""Domain-neutral approval state-machine entry points."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from sqlalchemy.orm import Session

from policy_api.approvals.definitions import (
    AssignmentKind,
    ProcessDefinition,
    StepAssignment,
    StepDefinition,
)
from policy_api.approvals.authorization import ApprovalAuthorizationPort
from policy_api.approvals.enums import (
    ApprovalCommandKind,
    ApprovalCommandStatus,
    ApprovalDecisionAction,
    ApprovalInstanceStatus,
    ApprovalTaskStatus,
)
from policy_api.approvals.models import (
    ApprovalCommandOperation,
    ApprovalDecision,
    ApprovalInstance,
    ApprovalTask,
)
from policy_api.approvals.repository import ApprovalRepository
from policy_api.models import User
from policy_api.tools.errors import ToolError


@dataclass(frozen=True, slots=True)
class ApprovalTransition:
    instance: ApprovalInstance
    tasks: Sequence[ApprovalTask]
    decision: ApprovalDecision | None
    operation: ApprovalCommandOperation
    replayed: bool


class ApprovalEngine:
    def __init__(self, repository: ApprovalRepository | None = None) -> None:
        self._repository = (
            repository if repository is not None else ApprovalRepository()
        )

    def start_instance(
        self,
        db: Session,
        *,
        definition: ProcessDefinition,
        subject_type: str,
        applicant_user_id: uuid.UUID,
        organization_unit_id: uuid.UUID,
        assignments: Mapping[str, StepAssignment],
        now: datetime,
    ) -> tuple[ApprovalInstance, Sequence[ApprovalTask]]:
        self._validate_assignments(definition, assignments)
        return self._repository.add_instance_with_tasks(
            db,
            definition=definition,
            subject_type=subject_type,
            applicant_user_id=applicant_user_id,
            organization_unit_id=organization_unit_id,
            assignments=assignments,
            now=now,
        )

    def approve_task(
        self,
        db: Session,
        *,
        task_id: uuid.UUID,
        actor: User,
        client_operation_id: uuid.UUID,
        comment: str | None,
        authorize: ApprovalAuthorizationPort,
        now: datetime,
    ) -> ApprovalTransition:
        return self._decide_task(
            db,
            task_id=task_id,
            actor=actor,
            client_operation_id=client_operation_id,
            action=ApprovalDecisionAction.APPROVE,
            comment=self._normalize_optional_comment(comment),
            authorize=authorize,
            now=now,
        )

    def reject_task(
        self,
        db: Session,
        *,
        task_id: uuid.UUID,
        actor: User,
        client_operation_id: uuid.UUID,
        comment: str,
        authorize: ApprovalAuthorizationPort,
        now: datetime,
    ) -> ApprovalTransition:
        normalized_comment = comment.strip()
        if not normalized_comment:
            raise ToolError("approval_decision_reason_required")
        return self._decide_task(
            db,
            task_id=task_id,
            actor=actor,
            client_operation_id=client_operation_id,
            action=ApprovalDecisionAction.REJECT,
            comment=normalized_comment,
            authorize=authorize,
            now=now,
        )

    def cancel_instance(
        self,
        db: Session,
        *,
        instance_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        client_operation_id: uuid.UUID,
        now: datetime,
    ) -> ApprovalTransition:
        observed_version = self._repository.resolve_instance_version(db, instance_id)
        if observed_version is None:
            raise ToolError("approval_instance_not_found")
        command_kind = ApprovalCommandKind.CANCEL
        payload_hash = self._payload_hash(
            command_kind=command_kind,
            instance_id=instance_id,
            task_id=None,
            comment=None,
        )
        instance = self._repository.lock_instance(db, instance_id)
        if instance is None:
            raise ToolError("approval_instance_not_found")
        operation, replayed = self._operation(
            db,
            actor_user_id=actor_user_id,
            client_operation_id=client_operation_id,
            command_kind=command_kind,
            instance_id=instance_id,
            task_id=None,
            payload_hash=payload_hash,
        )
        if replayed:
            return self._transition(db, instance, operation, None, replayed=True)
        if instance.status is not ApprovalInstanceStatus.RUNNING:
            raise ToolError("approval_instance_state_conflict")
        if instance.version != observed_version:
            raise ToolError("approval_instance_state_conflict")
        open_tasks = self._repository.lock_open_tasks(db, instance_id)
        if instance.applicant_user_id != actor_user_id:
            raise ToolError("approval_task_not_assigned")
        for task in open_tasks:
            task.status = ApprovalTaskStatus.CANCELLED
            task.completed_at = now
        instance.status = ApprovalInstanceStatus.CANCELLED
        instance.current_step_key = None
        instance.completed_at = now
        instance.version += 1
        self._complete_operation(operation, now)
        self._repository.flush(db)
        return self._transition(db, instance, operation, None, replayed=False)

    def _decide_task(
        self,
        db: Session,
        *,
        task_id: uuid.UUID,
        actor: User,
        client_operation_id: uuid.UUID,
        action: ApprovalDecisionAction,
        comment: str | None,
        authorize: ApprovalAuthorizationPort,
        now: datetime,
    ) -> ApprovalTransition:
        instance_id = self._repository.resolve_task_instance_id(db, task_id)
        if instance_id is None:
            raise ToolError("approval_task_not_found")
        instance = self._repository.lock_instance(db, instance_id)
        if instance is None:
            raise ToolError("approval_instance_not_found")
        command_kind = (
            ApprovalCommandKind.APPROVE
            if action is ApprovalDecisionAction.APPROVE
            else ApprovalCommandKind.REJECT
        )
        payload_hash = self._payload_hash(
            command_kind=command_kind,
            instance_id=instance_id,
            task_id=task_id,
            comment=comment,
        )
        operation, replayed = self._operation(
            db,
            actor_user_id=actor.id,
            client_operation_id=client_operation_id,
            command_kind=command_kind,
            instance_id=instance_id,
            task_id=task_id,
            payload_hash=payload_hash,
        )
        if replayed:
            return self._transition(db, instance, operation, None, replayed=True)
        if instance.status is not ApprovalInstanceStatus.RUNNING:
            raise ToolError("approval_instance_state_conflict")
        task = self._repository.lock_task(db, instance_id, task_id)
        if task is None:
            raise ToolError("approval_task_not_found")
        if (
            task.status is not ApprovalTaskStatus.PENDING
            or task.step_key != instance.current_step_key
        ):
            raise ToolError("approval_task_state_conflict")
        authorize.authorize(instance=instance, task=task, actor=actor, action=action)

        decision = ApprovalDecision(
            id=uuid.uuid4(),
            instance_id=instance_id,
            task_id=task_id,
            actor_user_id=actor.id,
            action=action,
            comment=comment,
            client_operation_id=client_operation_id,
            decided_at=now,
        )
        self._repository.add_decision(db, decision)
        task.status = (
            ApprovalTaskStatus.APPROVED
            if action is ApprovalDecisionAction.APPROVE
            else ApprovalTaskStatus.REJECTED
        )
        task.completed_at = now
        # The database enforces one pending task per instance with a partial
        # unique index. Persist completion before activating the successor so
        # SQLAlchemy update ordering cannot transiently create two pending rows.
        self._repository.flush(db)
        if action is ApprovalDecisionAction.REJECT:
            for unfinished in self._repository.lock_open_tasks(db, instance_id):
                if unfinished.id != task.id:
                    unfinished.status = ApprovalTaskStatus.CANCELLED
                    unfinished.completed_at = now
            self._finish_instance(instance, ApprovalInstanceStatus.REJECTED, now)
        else:
            waiting = next(
                (
                    candidate
                    for candidate in self._repository.lock_open_tasks(db, instance_id)
                    if candidate.status is ApprovalTaskStatus.WAITING
                ),
                None,
            )
            if waiting is None:
                self._finish_instance(instance, ApprovalInstanceStatus.APPROVED, now)
            else:
                waiting.status = ApprovalTaskStatus.PENDING
                waiting.activated_at = now
                instance.current_step_key = waiting.step_key
                instance.version += 1
        self._complete_operation(operation, now)
        self._repository.flush(db)
        return self._transition(db, instance, operation, decision, replayed=False)

    def _operation(
        self,
        db: Session,
        *,
        actor_user_id: uuid.UUID,
        client_operation_id: uuid.UUID,
        command_kind: ApprovalCommandKind,
        instance_id: uuid.UUID,
        task_id: uuid.UUID | None,
        payload_hash: str,
    ) -> tuple[ApprovalCommandOperation, bool]:
        operation = self._repository.get_command_operation(
            db, actor_user_id, client_operation_id
        )
        created = False
        if operation is None:
            operation, created = self._repository.claim_command_operation(
                db,
                actor_user_id=actor_user_id,
                client_operation_id=client_operation_id,
                command_kind=command_kind,
                instance_id=instance_id,
                task_id=task_id,
                canonical_payload_hash=payload_hash,
                status=ApprovalCommandStatus.IN_PROGRESS,
                completed_at=None,
            )
        if not self._operation_matches(
            operation, command_kind, instance_id, task_id, payload_hash
        ):
            raise ToolError("approval_operation_id_conflict")
        if not created:
            if operation.status is ApprovalCommandStatus.SUCCEEDED:
                return operation, True
            raise ToolError("approval_operation_id_conflict")
        return operation, False

    def _transition(
        self,
        db: Session,
        instance: ApprovalInstance,
        operation: ApprovalCommandOperation,
        decision: ApprovalDecision | None,
        *,
        replayed: bool,
    ) -> ApprovalTransition:
        return ApprovalTransition(
            instance=instance,
            tasks=self._repository.list_tasks(db, instance.id),
            decision=decision,
            operation=operation,
            replayed=replayed,
        )

    @staticmethod
    def _finish_instance(
        instance: ApprovalInstance, status: ApprovalInstanceStatus, now: datetime
    ) -> None:
        instance.status = status
        instance.current_step_key = None
        instance.completed_at = now
        instance.version += 1

    @staticmethod
    def _complete_operation(operation: ApprovalCommandOperation, now: datetime) -> None:
        operation.status = ApprovalCommandStatus.SUCCEEDED
        operation.completed_at = now

    @staticmethod
    def _operation_matches(
        operation: ApprovalCommandOperation,
        command_kind: ApprovalCommandKind,
        instance_id: uuid.UUID,
        task_id: uuid.UUID | None,
        payload_hash: str,
    ) -> bool:
        return (
            operation.command_kind is command_kind
            and operation.instance_id == instance_id
            and operation.task_id == task_id
            and operation.canonical_payload_hash == payload_hash
        )

    @staticmethod
    def _payload_hash(
        *,
        command_kind: ApprovalCommandKind,
        instance_id: uuid.UUID,
        task_id: uuid.UUID | None,
        comment: str | None,
    ) -> str:
        payload = {
            "command_kind": command_kind.value,
            "comment": comment,
            "instance_id": str(instance_id),
            "task_id": None if task_id is None else str(task_id),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _normalize_optional_comment(comment: str | None) -> str | None:
        if comment is None:
            return None
        normalized = comment.strip()
        return normalized or None

    @classmethod
    def _validate_assignments(
        cls,
        definition: ProcessDefinition,
        assignments: Mapping[str, StepAssignment],
    ) -> None:
        expected_keys = {step.key for step in definition.steps}
        if set(assignments) != expected_keys:
            raise ToolError("approval_assignment_mismatch")
        for step in definition.steps:
            if not cls._assignment_matches(step, assignments[step.key]):
                raise ToolError("approval_assignment_mismatch")

    @staticmethod
    def _assignment_matches(step: StepDefinition, assignment: StepAssignment) -> bool:
        if assignment.kind is not step.assignment_kind:
            return False
        if assignment.kind is AssignmentKind.USER:
            return (
                assignment.assigned_user_id is not None
                and assignment.required_capability is None
                and assignment.scope_organization_unit_id is None
            )
        return (
            assignment.assigned_user_id is None
            and assignment.required_capability == step.required_capability
            and assignment.scope_organization_unit_id is not None
        )
