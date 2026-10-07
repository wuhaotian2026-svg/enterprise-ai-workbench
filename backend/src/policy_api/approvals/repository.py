"""Persistence coordination for approval aggregates without transaction ownership."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from policy_api.approvals.definitions import (
    ProcessDefinition,
    StepAssignment,
    StepDefinition,
)
from policy_api.approvals.enums import (
    ApprovalCommandKind,
    ApprovalCommandStatus,
    ApprovalInstanceStatus,
    ApprovalTaskStatus,
)
from policy_api.approvals.models import (
    ApprovalCommandOperation,
    ApprovalDecision,
    ApprovalInstance,
    ApprovalTask,
)


class ApprovalRepository:
    def add_instance_with_tasks(
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
        instance = ApprovalInstance(
            id=uuid.uuid4(),
            process_key=definition.process_key,
            process_version=definition.version,
            subject_type=subject_type,
            applicant_user_id=applicant_user_id,
            organization_unit_id=organization_unit_id,
            status=ApprovalInstanceStatus.RUNNING,
            current_step_key=definition.steps[0].key,
            submitted_at=now,
            completed_at=None,
        )
        tasks = tuple(
            self._build_task(
                instance_id=instance.id,
                step=step,
                assignment=assignments[step.key],
                now=now,
                is_first=step.sequence == 1,
            )
            for step in definition.steps
        )

        db.add(instance)
        for task in tasks:
            db.add(task)
        db.flush()
        return instance, tasks

    def resolve_task_instance_id(
        self, db: Session, task_id: uuid.UUID
    ) -> uuid.UUID | None:
        return db.scalar(
            select(ApprovalTask.instance_id).where(ApprovalTask.id == task_id)
        )

    def lock_instance(
        self, db: Session, instance_id: uuid.UUID
    ) -> ApprovalInstance | None:
        return db.scalar(
            select(ApprovalInstance)
            .where(ApprovalInstance.id == instance_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )

    def resolve_instance_version(
        self, db: Session, instance_id: uuid.UUID
    ) -> int | None:
        return db.scalar(
            select(ApprovalInstance.version).where(ApprovalInstance.id == instance_id)
        )

    def get_command_operation(
        self,
        db: Session,
        actor_user_id: uuid.UUID,
        client_operation_id: uuid.UUID,
    ) -> ApprovalCommandOperation | None:
        return db.scalar(
            select(ApprovalCommandOperation)
            .where(
                ApprovalCommandOperation.actor_user_id == actor_user_id,
                ApprovalCommandOperation.client_operation_id == client_operation_id,
            )
            .execution_options(populate_existing=True)
        )

    def claim_command_operation(
        self,
        db: Session,
        *,
        actor_user_id: uuid.UUID,
        client_operation_id: uuid.UUID,
        command_kind: ApprovalCommandKind,
        instance_id: uuid.UUID,
        task_id: uuid.UUID | None,
        canonical_payload_hash: str,
        status: ApprovalCommandStatus,
        completed_at: datetime | None,
    ) -> tuple[ApprovalCommandOperation, bool]:
        operation_id = uuid.uuid4()
        claimed_id = db.scalar(
            insert(ApprovalCommandOperation)
            .values(
                id=operation_id,
                actor_user_id=actor_user_id,
                client_operation_id=client_operation_id,
                command_kind=command_kind,
                instance_id=instance_id,
                task_id=task_id,
                canonical_payload_hash=canonical_payload_hash,
                status=status,
                completed_at=completed_at,
            )
            .on_conflict_do_nothing(
                index_elements=["actor_user_id", "client_operation_id"]
            )
            .returning(ApprovalCommandOperation.id)
        )
        operation = db.scalar(
            select(ApprovalCommandOperation)
            .where(
                ApprovalCommandOperation.id
                == (claimed_id if claimed_id is not None else operation_id),
            )
            .execution_options(populate_existing=True)
        )
        if operation is None and claimed_id is None:
            operation = self.get_command_operation(
                db, actor_user_id, client_operation_id
            )
        if operation is None:
            raise RuntimeError("approval_operation_claim_failed")
        return operation, claimed_id is not None

    def lock_task(
        self, db: Session, instance_id: uuid.UUID, task_id: uuid.UUID
    ) -> ApprovalTask | None:
        return db.scalar(
            select(ApprovalTask)
            .where(
                ApprovalTask.id == task_id,
                ApprovalTask.instance_id == instance_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )

    def lock_open_tasks(
        self, db: Session, instance_id: uuid.UUID
    ) -> Sequence[ApprovalTask]:
        return tuple(
            db.scalars(
                select(ApprovalTask)
                .where(
                    ApprovalTask.instance_id == instance_id,
                    ApprovalTask.status.in_(
                        (ApprovalTaskStatus.WAITING, ApprovalTaskStatus.PENDING)
                    ),
                )
                .order_by(ApprovalTask.sequence)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        )

    def list_tasks(
        self, db: Session, instance_id: uuid.UUID
    ) -> Sequence[ApprovalTask]:
        return tuple(
            db.scalars(
                select(ApprovalTask)
                .where(ApprovalTask.instance_id == instance_id)
                .order_by(ApprovalTask.sequence)
                .execution_options(populate_existing=True)
            )
        )

    def add_decision(self, db: Session, decision: ApprovalDecision) -> None:
        db.add(decision)

    def flush(self, db: Session) -> None:
        db.flush()

    @staticmethod
    def _build_task(
        *,
        instance_id: uuid.UUID,
        step: StepDefinition,
        assignment: StepAssignment,
        now: datetime,
        is_first: bool,
    ) -> ApprovalTask:
        return ApprovalTask(
            id=uuid.uuid4(),
            instance_id=instance_id,
            sequence=step.sequence,
            step_key=step.key,
            step_label=step.label,
            assignment_kind=assignment.kind,
            assigned_user_id=assignment.assigned_user_id,
            required_capability=assignment.required_capability,
            scope_organization_unit_id=assignment.scope_organization_unit_id,
            status=(
                ApprovalTaskStatus.PENDING if is_first else ApprovalTaskStatus.WAITING
            ),
            activated_at=now if is_first else None,
            completed_at=None,
        )
