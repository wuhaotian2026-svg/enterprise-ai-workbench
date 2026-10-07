"""Persistence boundaries for the procurement submission aggregate."""

from __future__ import annotations

from collections.abc import Sequence
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from policy_api.approvals.models import (
    ApprovalDecision,
    ApprovalInstance,
    ApprovalTask,
)
from policy_api.hr.models import EmployeeProfile
from policy_api.models import User
from policy_api.procurement.enums import ProcurementCommandOperationStatus
from policy_api.procurement.models import (
    ProcurementCommandOperation,
    ProcurementRequest,
    ProcurementRequestItem,
)
from policy_api.workbench.capabilities import CapabilityGrant, OrganizationUnit
from policy_api.workbench.audit import SecurityAuditEvent
from policy_api.workbench.events import ProductEvent


class ProcurementRepository:
    """Coordinates rows without owning commit or rollback."""

    def get_active_profile_by_user(
        self,
        db: Session,
        user_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> EmployeeProfile | None:
        query = (
            select(EmployeeProfile)
            .where(
                EmployeeProfile.user_id == user_id,
                EmployeeProfile.is_active.is_(True),
            )
            .execution_options(populate_existing=True)
        )
        return db.scalar(query.with_for_update() if for_update else query)

    def get_active_organization(
        self, db: Session, organization_id: uuid.UUID
    ) -> OrganizationUnit | None:
        return db.scalar(
            select(OrganizationUnit)
            .where(
                OrganizationUnit.id == organization_id,
                OrganizationUnit.is_active.is_(True),
            )
            .execution_options(populate_existing=True)
        )

    def get_active_employee(
        self, db: Session, employee_id: uuid.UUID
    ) -> EmployeeProfile | None:
        return db.scalar(
            select(EmployeeProfile)
            .where(
                EmployeeProfile.id == employee_id,
                EmployeeProfile.is_active.is_(True),
            )
            .execution_options(populate_existing=True)
        )

    def get_active_user(
        self,
        db: Session,
        user_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> User | None:
        query = (
            select(User)
            .where(User.id == user_id, User.is_active.is_(True))
            .execution_options(populate_existing=True)
        )
        return db.scalar(query.with_for_update() if for_update else query)

    def get_active_capability_grants(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        capability: str,
        for_update: bool = False,
    ) -> Sequence[CapabilityGrant]:
        query = (
            select(CapabilityGrant)
            .where(
                CapabilityGrant.user_id == user_id,
                CapabilityGrant.capability == capability,
                CapabilityGrant.is_active.is_(True),
            )
            .order_by(CapabilityGrant.id)
            .execution_options(populate_existing=True)
        )
        return tuple(
            db.scalars(
                query.with_for_update() if for_update else query
            )
        )

    def claim_operation(
        self,
        db: Session,
        *,
        actor_user_id: uuid.UUID,
        client_operation_id: uuid.UUID,
        command_kind: str,
        canonical_payload_hash: str,
    ) -> tuple[ProcurementCommandOperation, bool]:
        operation_id = uuid.uuid4()
        claimed_id = db.scalar(
            insert(ProcurementCommandOperation)
            .values(
                id=operation_id,
                actor_user_id=actor_user_id,
                client_operation_id=client_operation_id,
                command_kind=command_kind,
                canonical_payload_hash=canonical_payload_hash,
                status=ProcurementCommandOperationStatus.IN_PROGRESS,
                result_resource_type=None,
                result_resource_id=None,
            )
            .on_conflict_do_nothing(
                index_elements=["actor_user_id", "client_operation_id"]
            )
            .returning(ProcurementCommandOperation.id)
        )
        if claimed_id is not None:
            operation = db.scalar(
                select(ProcurementCommandOperation)
                .where(ProcurementCommandOperation.id == claimed_id)
                .execution_options(populate_existing=True)
            )
        else:
            operation = self.get_operation(
                db, actor_user_id, client_operation_id
            )
        if operation is None:
            raise RuntimeError("procurement_operation_claim_failed")
        return operation, claimed_id is not None

    def get_operation(
        self,
        db: Session,
        actor_user_id: uuid.UUID,
        client_operation_id: uuid.UUID,
    ) -> ProcurementCommandOperation | None:
        return db.scalar(
            select(ProcurementCommandOperation)
            .where(
                ProcurementCommandOperation.actor_user_id == actor_user_id,
                ProcurementCommandOperation.client_operation_id
                == client_operation_id,
            )
            .execution_options(populate_existing=True)
        )

    def add_request(self, db: Session, request: ProcurementRequest) -> None:
        db.add(request)

    def add_request_item(
        self, db: Session, item: ProcurementRequestItem
    ) -> None:
        db.add(item)

    def complete_operation(
        self,
        operation: ProcurementCommandOperation,
        request_id: uuid.UUID,
    ) -> None:
        operation.status = ProcurementCommandOperationStatus.SUCCEEDED
        operation.result_resource_type = "procurement_request"
        operation.result_resource_id = request_id

    def get_request(
        self, db: Session, request_id: uuid.UUID
    ) -> ProcurementRequest | None:
        return db.scalar(
            select(ProcurementRequest)
            .where(ProcurementRequest.id == request_id)
            .execution_options(populate_existing=True)
        )

    def list_owner_requests(
        self,
        db: Session,
        *,
        applicant_employee_id: uuid.UUID,
        applicant_user_id: uuid.UUID,
    ) -> Sequence[tuple[ProcurementRequest, ApprovalInstance]]:
        return tuple(
            (request, instance)
            for request, instance in db.execute(
                select(ProcurementRequest, ApprovalInstance)
                .join(
                    ApprovalInstance,
                    ApprovalInstance.id
                    == ProcurementRequest.approval_instance_id,
                )
                .where(
                    ProcurementRequest.applicant_employee_id
                    == applicant_employee_id,
                    ApprovalInstance.applicant_user_id == applicant_user_id,
                )
                .order_by(
                    ProcurementRequest.submitted_at.desc(),
                    ProcurementRequest.id,
                )
                .execution_options(populate_existing=True)
            )
        )

    def get_owner_request(
        self,
        db: Session,
        *,
        request_id: uuid.UUID,
        applicant_employee_id: uuid.UUID,
        applicant_user_id: uuid.UUID,
    ) -> tuple[ProcurementRequest, ApprovalInstance] | None:
        row = db.execute(
            select(ProcurementRequest, ApprovalInstance)
            .join(
                ApprovalInstance,
                ApprovalInstance.id == ProcurementRequest.approval_instance_id,
            )
            .where(
                ProcurementRequest.id == request_id,
                ProcurementRequest.applicant_employee_id == applicant_employee_id,
                ApprovalInstance.applicant_user_id == applicant_user_id,
            )
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            return None
        return row[0], row[1]

    def get_request_by_instance(
        self, db: Session, instance_id: uuid.UUID
    ) -> ProcurementRequest | None:
        return db.scalar(
            select(ProcurementRequest)
            .where(ProcurementRequest.approval_instance_id == instance_id)
            .execution_options(populate_existing=True)
        )

    def list_items(
        self, db: Session, request_id: uuid.UUID
    ) -> Sequence[ProcurementRequestItem]:
        return tuple(
            db.scalars(
                select(ProcurementRequestItem)
                .where(ProcurementRequestItem.request_id == request_id)
                .order_by(ProcurementRequestItem.line_number)
                .execution_options(populate_existing=True)
            )
        )

    def get_instance(
        self, db: Session, instance_id: uuid.UUID
    ) -> ApprovalInstance | None:
        return db.scalar(
            select(ApprovalInstance)
            .where(ApprovalInstance.id == instance_id)
            .execution_options(populate_existing=True)
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

    def list_decision_facts(
        self, db: Session, instance_id: uuid.UUID
    ) -> Sequence[tuple[ApprovalDecision, ApprovalTask, str, str | None]]:
        return tuple(
            (decision, task, username, display_name)
            for decision, task, username, display_name in db.execute(
                select(
                    ApprovalDecision,
                    ApprovalTask,
                    User.username,
                    EmployeeProfile.display_name,
                )
                .join(ApprovalTask, ApprovalTask.id == ApprovalDecision.task_id)
                .join(User, User.id == ApprovalDecision.actor_user_id)
                .outerjoin(EmployeeProfile, EmployeeProfile.user_id == User.id)
                .where(ApprovalDecision.instance_id == instance_id)
                .order_by(ApprovalTask.sequence, ApprovalDecision.decided_at)
                .execution_options(populate_existing=True)
            )
        )

    def get_subject_identity(
        self,
        db: Session,
        *,
        applicant_user_id: uuid.UUID,
        organization_unit_id: uuid.UUID,
    ) -> tuple[str, str] | None:
        row = db.execute(
            select(
                User.username,
                EmployeeProfile.display_name,
                OrganizationUnit.name,
            )
            .select_from(User)
            .outerjoin(EmployeeProfile, EmployeeProfile.user_id == User.id)
            .join(
                OrganizationUnit,
                OrganizationUnit.id == organization_unit_id,
            )
            .where(User.id == applicant_user_id)
        ).one_or_none()
        if row is None:
            return None
        username, display_name, organization_display_name = row
        return display_name or username, organization_display_name

    def get_submission_audit(
        self,
        db: Session,
        actor_user_id: uuid.UUID,
        operation_id: uuid.UUID,
    ) -> SecurityAuditEvent | None:
        return db.scalar(
            select(SecurityAuditEvent)
            .where(
                SecurityAuditEvent.actor_user_id == actor_user_id,
                SecurityAuditEvent.operation_id == operation_id,
            )
            .execution_options(populate_existing=True)
        )

    def get_product_event(
        self, db: Session, event_id: uuid.UUID
    ) -> ProductEvent | None:
        return db.scalar(
            select(ProductEvent)
            .where(ProductEvent.event_id == event_id)
            .execution_options(populate_existing=True)
        )

    def flush(self, db: Session) -> None:
        db.flush()


__all__ = ["ProcurementRepository"]
