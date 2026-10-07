"""Atomic, command-idempotent procurement request submission."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import uuid

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
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
    ApprovalDecisionAction,
    ApprovalInstanceStatus,
    ApprovalTaskStatus,
)
from policy_api.approvals.models import (
    ApprovalDecision,
    ApprovalInstance,
    ApprovalTask,
)
from policy_api.approvals.schemas import (
    SubjectApplicant,
    SubjectDetail,
    SubjectItem,
    SubjectOrganization,
    SubjectSummary,
    SubjectTimelineEntry,
)
from policy_api.approvals.service import ApprovalEngine, ApprovalTransition
from policy_api.approvals.subject_adapter import ApprovalError
from policy_api.hr.models import EmployeeProfile
from policy_api.models import User
from policy_api.procurement.calculation import ProcurementTotal, calculate_total
from policy_api.procurement.enums import (
    ProcurementCategoryCode,
    ProcurementCommandOperationStatus,
    ProcurementCurrencyCode,
)
from policy_api.procurement.models import (
    ProcurementCommandOperation,
    ProcurementRequest,
    ProcurementRequestItem,
)
from policy_api.procurement.repository import ProcurementRepository
from policy_api.procurement.schemas import (
    ProcurementRequestInput,
    ProcurementRequestItemInput,
)
from policy_api.tools.errors import ToolError
from policy_api.workbench.audit import SecurityAuditEvent, append_security_audit
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityResolver,
    CapabilityScope,
    OrganizationUnit,
)
from policy_api.workbench.events import EventInput, ProductEvent, ProductEventEmitter
from policy_api.procurement.observability import (
    ProcurementObservability,
    attempt_outcome_for_code,
)


SUBMIT_COMMAND_KIND = "procurement.submit"
SUBJECT_TYPE = "procurement_request"
RESULT_RESOURCE_TYPE = "procurement_request"
_EVENT_NAMESPACE = uuid.UUID("c148842e-3282-44da-8a58-9d3144ac66ec")


def _require_utf8_text(value: str | None, *, code: str) -> None:
    if value is None:
        return
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(code) from None


PROCUREMENT_REQUEST_V1 = ProcessDefinition(
    process_key="procurement.request",
    version=1,
    steps=(
        StepDefinition(
            key="department_manager_review",
            label="部门负责人审批",
            sequence=1,
            assignment_kind=AssignmentKind.USER,
            required_capability=Capability.PROCUREMENT_DEPARTMENT_REVIEW.value,
        ),
        StepDefinition(
            key="procurement_review",
            label="采购专员复核",
            sequence=2,
            assignment_kind=AssignmentKind.CAPABILITY,
            required_capability=Capability.PROCUREMENT_FINAL_REVIEW.value,
        ),
    ),
)


def procurement_display_status(instance: ApprovalInstance) -> str:
    """Derive the only user-facing request status from the approval instance."""

    if instance.status is ApprovalInstanceStatus.RUNNING:
        if instance.current_step_key == "department_manager_review":
            return "pending_manager"
        if instance.current_step_key == "procurement_review":
            return "pending_procurement"
        raise ApprovalError("approval_subject_invalid")
    if instance.status in {
        ApprovalInstanceStatus.APPROVED,
        ApprovalInstanceStatus.REJECTED,
        ApprovalInstanceStatus.CANCELLED,
    }:
        return instance.status.value
    raise ApprovalError("approval_subject_invalid")


class ProcurementSubjectAdapter:
    """Read-only whitelist adapter for approval-center subject rendering."""

    subject_type = SUBJECT_TYPE

    def __init__(self, *, repository: ProcurementRepository | None = None) -> None:
        self._repository = repository or ProcurementRepository()

    def summary(
        self, db: Session, instance: ApprovalInstance
    ) -> SubjectSummary:
        request = self._request(db, instance)
        return self.summary_from_request(request, instance)

    @staticmethod
    def summary_from_request(
        request: ProcurementRequest, instance: ApprovalInstance
    ) -> SubjectSummary:
        ProcurementSubjectAdapter._validate_request_pair(request, instance)
        return SubjectSummary(
            request_number=request.request_number,
            title=request.title,
            total=request.total_amount,
            status=procurement_display_status(instance),
        )

    def detail(
        self, db: Session, instance: ApprovalInstance
    ) -> SubjectDetail:
        request = self._request(db, instance)
        identity = self._repository.get_subject_identity(
            db,
            applicant_user_id=instance.applicant_user_id,
            organization_unit_id=instance.organization_unit_id,
        )
        if identity is None:
            raise ApprovalError("approval_subject_invalid")
        applicant_name, organization_name = identity
        items = tuple(
            SubjectItem(
                category=(
                    item.category_code.value
                    if isinstance(item.category_code, ProcurementCategoryCode)
                    else str(item.category_code)
                ),
                name=item.item_name,
                specification=item.specification,
                quantity=item.quantity,
                unit=item.unit,
                unit_price=item.estimated_unit_price,
                subtotal=item.subtotal,
            )
            for item in self._repository.list_items(db, request.id)
        )
        timeline: list[SubjectTimelineEntry] = [
            SubjectTimelineEntry(
                kind="submitted",
                occurred_at=instance.submitted_at,
                step_key=None,
                step_label=None,
                action=None,
                actor_display_name=applicant_name,
                comment=None,
                status="submitted",
            )
        ]
        for decision, task, username, display_name in (
            self._repository.list_decision_facts(db, instance.id)
        ):
            timeline.append(
                SubjectTimelineEntry(
                    kind="decision",
                    occurred_at=decision.decided_at,
                    step_key=task.step_key,
                    step_label=task.step_label,
                    action=decision.action.value,
                    actor_display_name=display_name or username,
                    comment=decision.comment,
                    status=task.status.value,
                )
            )
        if instance.status is ApprovalInstanceStatus.CANCELLED:
            if instance.completed_at is None:
                raise ApprovalError("approval_subject_invalid")
            timeline.append(
                SubjectTimelineEntry(
                    kind="withdrawn",
                    occurred_at=instance.completed_at,
                    step_key=None,
                    step_label=None,
                    action=None,
                    actor_display_name=applicant_name,
                    comment=None,
                    status=instance.status.value,
                )
            )
        elif instance.status in {
            ApprovalInstanceStatus.APPROVED,
            ApprovalInstanceStatus.REJECTED,
        }:
            if instance.completed_at is None:
                raise ApprovalError("approval_subject_invalid")
            timeline.append(
                SubjectTimelineEntry(
                    kind="completed",
                    occurred_at=instance.completed_at,
                    step_key=None,
                    step_label=None,
                    action=None,
                    actor_display_name=None,
                    comment=None,
                    status=instance.status.value,
                )
            )
        return SubjectDetail(
            summary=self.summary_from_request(request, instance),
            purpose=request.purpose,
            needed_by_date=request.needed_by_date,
            currency=request.currency,
            items=items,
            applicant=SubjectApplicant(display_name=applicant_name),
            organization=SubjectOrganization(display_name=organization_name),
            timeline=tuple(timeline),
        )

    def _request(
        self, db: Session, instance: ApprovalInstance
    ) -> ProcurementRequest:
        request = self._repository.get_request_by_instance(db, instance.id)
        if request is None:
            raise ApprovalError("approval_subject_invalid")
        self._validate_request_pair(request, instance)
        return request

    @staticmethod
    def _validate_request_pair(
        request: ProcurementRequest, instance: ApprovalInstance
    ) -> None:
        if (
            instance.subject_type != SUBJECT_TYPE
            or instance.process_key != PROCUREMENT_REQUEST_V1.process_key
            or instance.process_version != PROCUREMENT_REQUEST_V1.version
        ):
            raise ApprovalError("approval_subject_invalid")
        if (
            request.organization_unit_id != instance.organization_unit_id
            or request.approval_instance_id != instance.id
        ):
            raise ApprovalError("approval_subject_invalid")


class _ProcurementDecisionAuthorization(ApprovalAuthorizationPort):
    def __init__(self, access: ProcurementApprovalAccess, db: Session) -> None:
        self._access = access
        self._db = db

    def authorize(
        self,
        *,
        instance: ApprovalInstance,
        task: ApprovalTask,
        actor: User,
        action: ApprovalDecisionAction,
    ) -> None:
        self._access.authorize_decision(
            self._db,
            instance=instance,
            task=task,
            actor=actor,
            action=action,
        )


@dataclass(frozen=True, slots=True)
class ProcurementApprovalReadContext:
    actor_id: uuid.UUID | None
    actor_is_active_employee: bool
    department_review_scope: CapabilityScope | None
    final_review_scope: CapabilityScope | None


class ProcurementApprovalAccess:
    """Procurement-owned visibility and transaction-time authorization."""

    def __init__(
        self,
        *,
        repository: ProcurementRepository | None = None,
        capability_resolver: CapabilityResolver | None = None,
    ) -> None:
        self._repository = repository or ProcurementRepository()
        self._capability_resolver = capability_resolver or CapabilityResolver()

    def authorization(self, db: Session) -> ApprovalAuthorizationPort:
        return _ProcurementDecisionAuthorization(self, db)

    def read_context(
        self,
        db: Session,
        *,
        actor: User,
    ) -> ProcurementApprovalReadContext:
        """Resolve one immutable authorization snapshot for a list request.

        The context never crosses requests or sessions. It narrows SQL
        candidates and lets ``can_view`` repeat deterministic domain checks
        without issuing the same authorization queries for every row.
        ``authorize_decision`` still reloads locked facts before every write.
        """

        actor_id = getattr(actor, "id", None)
        if not isinstance(actor_id, uuid.UUID):
            return ProcurementApprovalReadContext(
                actor_id=None,
                actor_is_active_employee=False,
                department_review_scope=None,
                final_review_scope=None,
            )
        authoritative_actor = self._repository.get_active_user(
            db,
            actor_id,
            for_update=False,
        )
        if authoritative_actor is None:
            return ProcurementApprovalReadContext(
                actor_id=actor_id,
                actor_is_active_employee=False,
                department_review_scope=None,
                final_review_scope=None,
            )
        profile = self._repository.get_active_profile_by_user(
            db,
            authoritative_actor.id,
            for_update=False,
        )
        if profile is None:
            return ProcurementApprovalReadContext(
                actor_id=actor_id,
                actor_is_active_employee=False,
                department_review_scope=None,
                final_review_scope=None,
            )
        return ProcurementApprovalReadContext(
            actor_id=authoritative_actor.id,
            actor_is_active_employee=True,
            department_review_scope=self._capability_resolver.scope_for(
                db,
                authoritative_actor,
                Capability.PROCUREMENT_DEPARTMENT_REVIEW,
            ),
            final_review_scope=self._capability_resolver.scope_for(
                db,
                authoritative_actor,
                Capability.PROCUREMENT_FINAL_REVIEW,
            ),
        )

    def can_view(
        self,
        db: Session,
        *,
        actor: User,
        instance: ApprovalInstance,
        task: ApprovalTask,
        decision: ApprovalDecision | None,
        read_context: ProcurementApprovalReadContext | None = None,
    ) -> bool:
        if not self._is_procurement_task(instance, task):
            return False
        if task.status is ApprovalTaskStatus.PENDING:
            if read_context is not None:
                return self._pending_visible_from_context(
                    actor=actor,
                    instance=instance,
                    task=task,
                    read_context=read_context,
                )
            try:
                self.authorize_decision(
                    db,
                    instance=instance,
                    task=task,
                    actor=actor,
                    action=ApprovalDecisionAction.APPROVE,
                    lock_facts=False,
                )
            except ToolError:
                return False
            return True
        if task.status in {
            ApprovalTaskStatus.APPROVED,
            ApprovalTaskStatus.REJECTED,
        }:
            return decision is not None and decision.actor_user_id == actor.id
        return False

    @staticmethod
    def _pending_visible_from_context(
        *,
        actor: User,
        instance: ApprovalInstance,
        task: ApprovalTask,
        read_context: ProcurementApprovalReadContext,
    ) -> bool:
        actor_id = getattr(actor, "id", None)
        if (
            not read_context.actor_is_active_employee
            or not isinstance(actor_id, uuid.UUID)
            or read_context.actor_id != actor_id
        ):
            return False
        if task.step_key == "department_manager_review":
            if (
                task.assignment_kind is not AssignmentKind.USER
                or task.assigned_user_id != actor_id
            ):
                return False
            scope = read_context.department_review_scope
        elif task.step_key == "procurement_review":
            if (
                task.assignment_kind is not AssignmentKind.CAPABILITY
                or task.assigned_user_id is not None
                or task.required_capability
                != Capability.PROCUREMENT_FINAL_REVIEW.value
                or task.scope_organization_unit_id
                != instance.organization_unit_id
            ):
                return False
            scope = read_context.final_review_scope
        else:
            return False
        return scope is not None and (
            scope.is_global
            or instance.organization_unit_id in scope.organization_unit_ids
        )

    def authorize_decision(
        self,
        db: Session,
        *,
        instance: ApprovalInstance,
        task: ApprovalTask,
        actor: User,
        action: ApprovalDecisionAction,
        lock_facts: bool = True,
    ) -> None:
        del action
        if not self._is_procurement_task(instance, task):
            raise ToolError("approval_task_not_assigned")
        actor_id = getattr(actor, "id", None)
        if not isinstance(actor_id, uuid.UUID):
            raise ToolError("approval_capability_required")
        authoritative_actor = self._repository.get_active_user(
            db, actor_id, for_update=lock_facts
        )
        if authoritative_actor is None:
            raise ToolError("approval_capability_required")
        if task.step_key == "department_manager_review":
            if (
                task.assignment_kind is not AssignmentKind.USER
                or task.assigned_user_id != authoritative_actor.id
            ):
                raise ToolError("approval_task_not_assigned")
            capability = Capability.PROCUREMENT_DEPARTMENT_REVIEW
        elif task.step_key == "procurement_review":
            if (
                task.assignment_kind is not AssignmentKind.CAPABILITY
                or task.assigned_user_id is not None
                or task.required_capability
                != Capability.PROCUREMENT_FINAL_REVIEW.value
            ):
                raise ToolError("approval_task_not_assigned")
            capability = Capability.PROCUREMENT_FINAL_REVIEW
        else:
            raise ToolError("approval_task_not_assigned")
        profile = self._repository.get_active_profile_by_user(
            db, authoritative_actor.id, for_update=lock_facts
        )
        if profile is None:
            raise ToolError("approval_capability_required")
        self._repository.get_active_capability_grants(
            db,
            user_id=authoritative_actor.id,
            capability=capability.value,
            for_update=lock_facts,
        )
        scope = self._capability_resolver.scope_for(
            db, authoritative_actor, capability
        )
        if scope is None:
            raise ToolError("approval_capability_required")
        if (
            not scope.is_global
            and instance.organization_unit_id not in scope.organization_unit_ids
        ):
            raise ToolError("approval_scope_denied")

    @staticmethod
    def _is_procurement_task(
        instance: ApprovalInstance, task: ApprovalTask
    ) -> bool:
        return (
            instance.subject_type == SUBJECT_TYPE
            and instance.process_key == PROCUREMENT_REQUEST_V1.process_key
            and instance.process_version == PROCUREMENT_REQUEST_V1.version
            and task.instance_id == instance.id
            and task.step_key
            in {"department_manager_review", "procurement_review"}
        )


@dataclass(frozen=True, slots=True)
class ProcurementItemCommand:
    category_code: ProcurementCategoryCode
    item_name: str
    specification: str | None
    quantity: Decimal
    unit: str
    estimated_unit_price: Decimal

    def __post_init__(self) -> None:
        if type(self.category_code) is not ProcurementCategoryCode:
            raise ValueError("procurement_item_invalid")
        if (
            type(self.item_name) is not str
            or (
                self.specification is not None
                and type(self.specification) is not str
            )
            or type(self.unit) is not str
        ):
            raise ValueError("procurement_item_invalid")
        if type(self.quantity) is not Decimal or type(
            self.estimated_unit_price
        ) is not Decimal:
            raise ValueError("procurement_amount_invalid")
        try:
            normalized = ProcurementRequestItemInput.model_validate(
                {
                    "category_code": self.category_code,
                    "item_name": self.item_name,
                    "specification": self.specification,
                    "quantity": self.quantity,
                    "unit": self.unit,
                    "estimated_unit_price": self.estimated_unit_price,
                }
            )
        except ValidationError as error:
            amount_fields = {"quantity", "estimated_unit_price"}
            code = (
                "procurement_amount_invalid"
                if any(
                    details.get("loc", (None,))[0] in amount_fields
                    for details in error.errors()
                )
                else "procurement_item_invalid"
            )
            raise ValueError(code) from None
        for value in (
            normalized.item_name,
            normalized.specification,
            normalized.unit,
        ):
            _require_utf8_text(value, code="procurement_item_invalid")
        object.__setattr__(self, "item_name", normalized.item_name)
        object.__setattr__(self, "specification", normalized.specification)
        object.__setattr__(self, "quantity", normalized.quantity)
        object.__setattr__(self, "unit", normalized.unit)
        object.__setattr__(
            self, "estimated_unit_price", normalized.estimated_unit_price
        )

    @classmethod
    def _from_normalized(
        cls, normalized: ProcurementRequestItemInput
    ) -> ProcurementItemCommand:
        return cls(
            category_code=normalized.category_code,
            item_name=normalized.item_name,
            specification=normalized.specification,
            quantity=normalized.quantity,
            unit=normalized.unit,
            estimated_unit_price=normalized.estimated_unit_price,
        )


@dataclass(frozen=True, slots=True)
class SubmitProcurementRequestCommand:
    title: str
    purpose: str
    needed_by_date: date
    currency: ProcurementCurrencyCode
    items: tuple[ProcurementItemCommand, ...]

    def __post_init__(self) -> None:
        if type(self.title) is not str or type(self.purpose) is not str:
            raise ValueError("procurement_request_invalid")
        if type(self.needed_by_date) is not date:
            raise ValueError("procurement_needed_date_invalid")
        if type(self.currency) is not ProcurementCurrencyCode:
            raise ValueError("procurement_request_invalid")
        if not isinstance(self.items, (list, tuple)):
            raise ValueError("procurement_items_required")
        frozen_items = tuple(self.items)
        if not 1 <= len(frozen_items) <= 50:
            raise ValueError("procurement_items_required")
        if any(type(item) is not ProcurementItemCommand for item in frozen_items):
            raise ValueError("procurement_item_invalid")
        try:
            normalized = ProcurementRequestInput.model_validate(
                {
                    "title": self.title,
                    "purpose": self.purpose,
                    "needed_by_date": self.needed_by_date,
                    "currency": self.currency,
                    "items": [
                        {
                            "category_code": item.category_code,
                            "item_name": item.item_name,
                            "specification": item.specification,
                            "quantity": item.quantity,
                            "unit": item.unit,
                            "estimated_unit_price": item.estimated_unit_price,
                        }
                        for item in frozen_items
                    ],
                },
                context={"today": self.needed_by_date},
            )
        except ValidationError:
            raise ValueError("procurement_request_invalid") from None
        _require_utf8_text(normalized.title, code="procurement_request_invalid")
        _require_utf8_text(normalized.purpose, code="procurement_request_invalid")
        normalized_items = tuple(
            ProcurementItemCommand._from_normalized(item)
            for item in normalized.items
        )
        object.__setattr__(self, "title", normalized.title)
        object.__setattr__(self, "purpose", normalized.purpose)
        object.__setattr__(self, "items", normalized_items)


@dataclass(frozen=True, slots=True)
class ProcurementRequestResult:
    request: ProcurementRequest
    items: Sequence[ProcurementRequestItem]
    instance: ApprovalInstance
    tasks: Sequence[ApprovalTask]
    operation: ProcurementCommandOperation
    replayed: bool


def _canonical_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("canonical_decimal_invalid")
    if value.is_zero():
        return "0"
    sign, raw_digits, exponent = value.as_tuple()
    digits = list(raw_digits)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    number = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        number += "0" * exponent
    else:
        point = len(number) + exponent
        number = (
            f"{number[:point]}.{number[point:]}"
            if point > 0
            else f"0.{('0' * -point)}{number}"
        )
    return f"-{number}" if sign else number


def canonical_submission_hash(command: SubmitProcurementRequestCommand) -> str:
    if type(command) is not SubmitProcurementRequestCommand:
        raise ValueError("procurement_request_invalid")
    payload = {
        "currency": command.currency.value,
        "items": [
            {
                "category_code": item.category_code.value,
                "estimated_unit_price": _canonical_decimal(
                    item.estimated_unit_price
                ),
                "item_name": item.item_name,
                "quantity": _canonical_decimal(item.quantity),
                "specification": item.specification,
                "unit": item.unit,
            }
            for item in command.items
        ],
        "needed_by_date": command.needed_by_date.isoformat(),
        "purpose": command.purpose,
        "title": command.title,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def item_count_bucket(count: int) -> str:
    if type(count) is not int or count < 1 or count > 50:
        raise ValueError("item_count_invalid")
    if count == 1:
        return "1"
    if count <= 5:
        return "2_5"
    if count <= 10:
        return "6_10"
    return "11_plus"


def amount_bucket(amount: Decimal) -> str:
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
        raise ValueError("amount_invalid")
    if amount < Decimal("1000"):
        return "0_999"
    if amount < Decimal("10000"):
        return "1000_9999"
    if amount < Decimal("100000"):
        return "10000_99999"
    return "100000_plus"


@dataclass(frozen=True, slots=True)
class ProcurementSubmitPreflight:
    authoritative_actor: User
    profile: EmployeeProfile
    organization: OrganizationUnit
    manager: User


class ProcurementService:
    def __init__(
        self,
        *,
        repository: ProcurementRepository | None = None,
        approval_engine: ApprovalEngine | None = None,
        capability_resolver: CapabilityResolver | None = None,
        product_event_emitter: ProductEventEmitter | None = None,
        audit_appender: Callable[..., object] = append_security_audit,
        now_factory: Callable[[], datetime] | None = None,
        request_number_factory: Callable[[], str] | None = None,
        observability: ProcurementObservability | None = None,
    ) -> None:
        self._repository = repository or ProcurementRepository()
        self._approval_engine = approval_engine or ApprovalEngine()
        self._capability_resolver = capability_resolver or CapabilityResolver()
        self._product_event_emitter = (
            product_event_emitter or ProductEventEmitter()
        )
        self._audit_appender = audit_appender
        self._observability = observability
        self._now_factory = now_factory or (
            lambda: datetime.now(timezone.utc)
        )
        self._request_number_factory = request_number_factory or (
            lambda: f"PR-{uuid.uuid4().hex.upper()}"
        )

    def list_my_requests(
        self, db: Session, *, actor: User
    ) -> Sequence[SubjectSummary]:
        authoritative_actor, profile = self._owner_context(db, actor)
        return tuple(
            ProcurementSubjectAdapter.summary_from_request(request, instance)
            for request, instance in self._repository.list_owner_requests(
                db,
                applicant_employee_id=profile.id,
                applicant_user_id=authoritative_actor.id,
            )
        )

    def get_my_request(
        self,
        db: Session,
        *,
        actor: User,
        request_id: uuid.UUID,
    ) -> SubjectDetail:
        authoritative_actor, profile = self._owner_context(db, actor)
        owned = self._repository.get_owner_request(
            db,
            request_id=request_id,
            applicant_employee_id=profile.id,
            applicant_user_id=authoritative_actor.id,
        )
        if owned is None:
            raise ToolError("procurement_request_not_found")
        _, instance = owned
        return ProcurementSubjectAdapter(
            repository=self._repository
        ).detail(db, instance)

    def withdraw_request(
        self,
        db: Session,
        *,
        actor: User,
        request_id: uuid.UUID,
        client_operation_id: uuid.UUID,
        audit_request_id: str | None = None,
        channel: str = "manual",
        commit: bool = True,
    ) -> ApprovalTransition:
        try:
            authoritative_actor, profile = self._owner_context(db, actor)
            owned = self._repository.get_owner_request(
                db,
                request_id=request_id,
                applicant_employee_id=profile.id,
                applicant_user_id=authoritative_actor.id,
            )
            if owned is None:
                raise ToolError("procurement_request_not_found")
            _, instance = owned
            occurred_at = self._now_factory()
            transition = self._approval_engine.cancel_instance(
                db,
                instance_id=instance.id,
                actor_user_id=authoritative_actor.id,
                client_operation_id=client_operation_id,
                now=occurred_at,
            )
            if self._observability is not None and not transition.replayed:
                self._observability.stage_transition(
                    db, actor_user_id=authoritative_actor.id,
                    actor_role=authoritative_actor.role.value,
                    transition=transition,
                    command_kind=ApprovalCommandKind.CANCEL,
                    occurred_at=occurred_at, request_id=audit_request_id,
                    channel=channel,
                )
            elif self._observability is not None and audit_request_id is not None:
                self._observability.stage_replayed_transition(
                    db, actor_user_id=authoritative_actor.id,
                    actor_role=authoritative_actor.role.value,
                    transition=transition,
                    command_kind=ApprovalCommandKind.CANCEL,
                    server_attempt_id=audit_request_id, stage="withdrawal",
                )
            if commit:
                db.commit()
            return transition
        except Exception as exc:
            if commit:
                db.rollback()
                self._record_failure_attempt(
                    db, exc=exc, actor=actor, client_operation_id=client_operation_id,
                    server_attempt_id=audit_request_id, command_kind="approval.cancel",
                    stage="withdrawal", procurement_request_id=request_id,
                )
            raise

    def _owner_context(
        self, db: Session, actor: User
    ) -> tuple[User, EmployeeProfile]:
        actor_id = getattr(actor, "id", None)
        if not isinstance(actor_id, uuid.UUID):
            raise ToolError("procurement_profile_required")
        authoritative_actor = self._repository.get_active_user(db, actor_id)
        if authoritative_actor is None:
            raise ToolError("procurement_profile_required")
        profile = self._repository.get_active_profile_by_user(
            db, authoritative_actor.id
        )
        if profile is None:
            raise ToolError("procurement_profile_required")
        return authoritative_actor, profile

    def preflight_submit(
        self, db: Session, *, actor: User
    ) -> ProcurementSubmitPreflight:
        """Validate authoritative submit prerequisites without staging writes."""
        actor_id = getattr(actor, "id", None)
        if not isinstance(actor_id, uuid.UUID):
            raise ToolError("procurement_profile_required")
        authoritative_actor = self._repository.get_active_user(db, actor_id)
        if authoritative_actor is None:
            raise ToolError("procurement_profile_required")
        profile = self._repository.get_active_profile_by_user(
            db, authoritative_actor.id
        )
        if profile is None:
            raise ToolError("procurement_profile_required")
        applicant_scope = self._capability_resolver.scope_for(
            db,
            authoritative_actor,
            Capability.PROCUREMENT_REQUEST_SELF_SERVICE,
        )
        if applicant_scope is None or profile.organization_unit_id is None:
            raise ToolError("procurement_profile_required")
        organization = self._repository.get_active_organization(
            db, profile.organization_unit_id
        )
        if organization is None or (
            not applicant_scope.is_global
            and organization.id not in applicant_scope.organization_unit_ids
        ):
            raise ToolError("procurement_profile_required")
        if profile.manager_employee_id is None:
            raise ToolError("procurement_manager_unavailable")
        manager_profile = self._repository.get_active_employee(
            db, profile.manager_employee_id
        )
        if manager_profile is None:
            raise ToolError("procurement_manager_unavailable")
        manager = self._repository.get_active_user(db, manager_profile.user_id)
        if manager is None:
            raise ToolError("procurement_manager_unavailable")
        manager_scope = self._capability_resolver.scope_for(
            db, manager, Capability.PROCUREMENT_DEPARTMENT_REVIEW
        )
        if manager_scope is None or (
            not manager_scope.is_global
            and organization.id not in manager_scope.organization_unit_ids
        ):
            raise ToolError("procurement_manager_capability_required")
        return ProcurementSubmitPreflight(
            authoritative_actor=authoritative_actor,
            profile=profile,
            organization=organization,
            manager=manager,
        )

    def submit_procurement_request(
        self,
        db: Session,
        *,
        actor: User,
        client_operation_id: uuid.UUID,
        command: SubmitProcurementRequestCommand,
        request_id: str | None,
        channel: str = "manual",
        commit: bool = True,
    ) -> ProcurementRequestResult:
        try:
            now = self._now_factory()
            command_snapshot = self._submission_snapshot(command)
            result = self._stage_submission(
                db,
                actor=actor,
                client_operation_id=client_operation_id,
                command=command_snapshot,
                request_id=request_id,
                channel=channel,
                now=now,
            )
            if result.replayed and self._observability is not None and request_id is not None:
                self._observability.stage_attempt(
                    db, actor_user_id=result.operation.actor_user_id,
                    actor_role=actor.role.value,
                    client_operation_id=client_operation_id,
                    server_attempt_id=request_id,
                    command_kind=SUBMIT_COMMAND_KIND, outcome="replayed",
                    code="exact_replay", stage="submission",
                    request_id=result.request.id,
                    organization_unit_id=result.request.organization_unit_id,
                )
            if commit:
                db.commit()
        except IntegrityError as exc:
            if commit:
                db.rollback()
                self._record_failure_attempt(
                    db, exc=ToolError("procurement_request_conflict"), actor=actor,
                    client_operation_id=client_operation_id,
                    server_attempt_id=request_id, command_kind=SUBMIT_COMMAND_KIND,
                    stage="submission", procurement_request_id=None,
                )
            raise ToolError("procurement_request_conflict") from None
        except Exception as exc:
            if commit:
                db.rollback()
                self._record_failure_attempt(
                    db, exc=exc, actor=actor,
                    client_operation_id=client_operation_id,
                    server_attempt_id=request_id, command_kind=SUBMIT_COMMAND_KIND,
                    stage="submission", procurement_request_id=None,
                )
            raise
        return result

    def _record_failure_attempt(
        self, db: Session, *, exc: Exception, actor: User,
        client_operation_id: uuid.UUID, server_attempt_id: str | None,
        command_kind: str, stage: str,
        procurement_request_id: uuid.UUID | None,
    ) -> None:
        if self._observability is None or server_attempt_id is None or not isinstance(exc, ToolError):
            return
        outcome = attempt_outcome_for_code(exc.code)
        if outcome is None:
            return
        request = self._repository.get_request(db, procurement_request_id) if procurement_request_id is not None else None
        try:
            self._observability.stage_attempt(
                db, actor_user_id=actor.id, actor_role=actor.role.value,
                client_operation_id=client_operation_id,
                server_attempt_id=server_attempt_id,
                command_kind=command_kind, outcome=outcome, code=exc.code,
                stage=stage, request_id=request.id if request is not None else None,
                organization_unit_id=request.organization_unit_id if request is not None else None,
            )
            db.commit()
        except Exception as evidence_error:
            db.rollback()
            exc.add_note(f"procurement evidence failed: {type(evidence_error).__name__}")

    def _stage_submission(
        self,
        db: Session,
        *,
        actor: User,
        client_operation_id: uuid.UUID,
        command: SubmitProcurementRequestCommand,
        request_id: str | None,
        channel: str,
        now: datetime,
    ) -> ProcurementRequestResult:
        preflight = self.preflight_submit(db, actor=actor)
        authoritative_actor = preflight.authoritative_actor
        profile = preflight.profile
        organization = preflight.organization
        manager = preflight.manager

        payload_hash = canonical_submission_hash(command)
        operation, claimed = self._repository.claim_operation(
            db,
            actor_user_id=authoritative_actor.id,
            client_operation_id=client_operation_id,
            command_kind=SUBMIT_COMMAND_KIND,
            canonical_payload_hash=payload_hash,
        )
        if not claimed:
            return self._replay(
                db,
                operation=operation,
                actor=authoritative_actor,
                profile_id=profile.id,
                organization_id=organization.id,
                payload_hash=payload_hash,
                command=command,
            )
        if command.needed_by_date < now.date():
            raise ToolError("procurement_needed_date_invalid")

        calculated = calculate_total(command.items)
        instance, tasks = self._approval_engine.start_instance(
            db,
            definition=PROCUREMENT_REQUEST_V1,
            subject_type=SUBJECT_TYPE,
            applicant_user_id=authoritative_actor.id,
            organization_unit_id=organization.id,
            assignments={
                "department_manager_review": StepAssignment.for_user(manager.id),
                "procurement_review": StepAssignment.for_capability(
                    Capability.PROCUREMENT_FINAL_REVIEW.value,
                    organization.id,
                ),
            },
            now=now,
        )
        business_number = self._request_number_factory()
        if (
            not isinstance(business_number, str)
            or not business_number
            or len(business_number) > 40
        ):
            raise ToolError("procurement_request_number_invalid")
        procurement_request = ProcurementRequest(
            id=uuid.uuid4(),
            request_number=business_number,
            approval_instance_id=instance.id,
            applicant_employee_id=profile.id,
            organization_unit_id=organization.id,
            title=command.title,
            purpose=command.purpose,
            needed_by_date=command.needed_by_date,
            currency=command.currency.value,
            total_amount=calculated.total_amount,
            submitted_at=now,
        )
        self._repository.add_request(db, procurement_request)
        items = tuple(
            ProcurementRequestItem(
                id=uuid.uuid4(),
                request_id=procurement_request.id,
                line_number=line_number,
                category_code=item.category_code,
                item_name=item.item_name,
                specification=item.specification,
                quantity=item.quantity,
                unit=item.unit,
                estimated_unit_price=item.estimated_unit_price,
                subtotal=calculated.subtotals[line_number - 1],
            )
            for line_number, item in enumerate(command.items, start=1)
        )
        for item in items:
            self._repository.add_request_item(db, item)

        count_bucket = item_count_bucket(len(items))
        total_bucket = amount_bucket(calculated.total_amount)
        summary = {
            "procurement_request_id": procurement_request.id,
            "approval_instance_id": instance.id,
            "organization_unit_id": organization.id,
            "item_count_bucket": count_bucket,
            "amount_bucket": total_bucket,
        }
        self._audit_appender(
            db,
            event_name="procurement_request_submitted",
            actor_user_id=authoritative_actor.id,
            target_type=RESULT_RESOURCE_TYPE,
            target_id=procurement_request.id,
            operation_id=client_operation_id,
            outcome="succeeded",
            request_id=request_id,
            summary=summary,
        )
        event_id = self._submission_event_id(
            authoritative_actor.id, client_operation_id
        )
        self._product_event_emitter.append(
            db,
            EventInput(
                event_id=event_id,
                event_name="procurement_request_submitted",
                module_key="procurement",
                actor_user_id=authoritative_actor.id,
                organization_unit_id=organization.id,
                role_snapshot=authoritative_actor.role.value,
                request_id=request_id,
                outcome="succeeded",
                duration_ms=None,
                dimensions={
                    "stage": "submission",
                    "channel": channel,
                    "item_count_bucket": count_bucket,
                    "amount_bucket": total_bucket,
                },
            ),
        )
        self._repository.complete_operation(operation, procurement_request.id)
        self._repository.flush(db)
        return ProcurementRequestResult(
            request=procurement_request,
            items=items,
            instance=instance,
            tasks=tuple(tasks),
            operation=operation,
            replayed=False,
        )

    def _replay(
        self,
        db: Session,
        *,
        operation: ProcurementCommandOperation,
        actor: User,
        profile_id: uuid.UUID,
        organization_id: uuid.UUID,
        payload_hash: str,
        command: SubmitProcurementRequestCommand,
    ) -> ProcurementRequestResult:
        if not (
            operation.command_kind == SUBMIT_COMMAND_KIND
            and operation.canonical_payload_hash == payload_hash
            and operation.status is ProcurementCommandOperationStatus.SUCCEEDED
            and operation.result_resource_type == RESULT_RESOURCE_TYPE
            and operation.result_resource_id is not None
        ):
            raise ToolError("operation_id_conflict")
        procurement_request = self._repository.get_request(
            db, operation.result_resource_id
        )
        if procurement_request is None:
            raise ToolError("operation_id_conflict")
        instance = self._repository.get_instance(
            db, procurement_request.approval_instance_id
        )
        if instance is None or not (
            procurement_request.applicant_employee_id == profile_id
            and procurement_request.organization_unit_id == organization_id
            and instance.applicant_user_id == actor.id
            and instance.organization_unit_id == organization_id
        ):
            raise ToolError("operation_id_conflict")
        items = tuple(
            self._repository.list_items(db, procurement_request.id)
        )
        tasks = tuple(self._repository.list_tasks(db, instance.id))
        calculated = calculate_total(command.items)
        count_bucket = item_count_bucket(len(command.items))
        total_bucket = amount_bucket(calculated.total_amount)
        audit = self._repository.get_submission_audit(
            db, actor.id, operation.client_operation_id
        )
        event_id = self._submission_event_id(
            actor.id, operation.client_operation_id
        )
        event = self._repository.get_product_event(db, event_id)
        if not (
            self._request_matches(
                procurement_request,
                command=command,
                calculated=calculated,
                profile_id=profile_id,
                organization_id=organization_id,
            )
            and self._items_match(
                items,
                request_id=procurement_request.id,
                command=command,
                subtotals=calculated.subtotals,
            )
            and self._instance_matches(
                instance,
                actor_user_id=actor.id,
                organization_id=organization_id,
            )
            and self._tasks_match(
                tasks,
                instance_id=instance.id,
                organization_id=organization_id,
            )
            and self._audit_matches(
                audit,
                actor_user_id=actor.id,
                operation_id=operation.client_operation_id,
                request=procurement_request,
                instance=instance,
                organization_id=organization_id,
                count_bucket=count_bucket,
                total_bucket=total_bucket,
            )
            and self._event_matches(
                event,
                event_id=event_id,
                actor_user_id=actor.id,
                organization_id=organization_id,
                count_bucket=count_bucket,
                total_bucket=total_bucket,
            )
        ):
            raise ToolError("operation_id_conflict")
        return ProcurementRequestResult(
            request=procurement_request,
            items=items,
            instance=instance,
            tasks=tasks,
            operation=operation,
            replayed=True,
        )

    @staticmethod
    def _submission_snapshot(
        command: SubmitProcurementRequestCommand,
    ) -> SubmitProcurementRequestCommand:
        if type(command) is not SubmitProcurementRequestCommand:
            raise ToolError("procurement_request_invalid")
        try:
            snapshot = SubmitProcurementRequestCommand(
                title=command.title,
                purpose=command.purpose,
                needed_by_date=command.needed_by_date,
                currency=command.currency,
                items=command.items,
            )
        except ValueError as error:
            code = str(error)
            if code not in {
                "procurement_items_required",
                "procurement_item_invalid",
                "procurement_needed_date_invalid",
                "procurement_amount_invalid",
                "procurement_request_invalid",
            }:
                code = "procurement_request_invalid"
            raise ToolError(code) from None
        except (AttributeError, TypeError, ValidationError):
            raise ToolError("procurement_request_invalid") from None
        return snapshot

    @staticmethod
    def _submission_event_id(
        actor_user_id: uuid.UUID, operation_id: uuid.UUID
    ) -> uuid.UUID:
        return uuid.uuid5(
            _EVENT_NAMESPACE,
            f"{actor_user_id}:{operation_id}:procurement_request_submitted",
        )

    @staticmethod
    def _request_matches(
        request: ProcurementRequest,
        *,
        command: SubmitProcurementRequestCommand,
        calculated: ProcurementTotal,
        profile_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> bool:
        return (
            type(request.request_number) is str
            and 1 <= len(request.request_number) <= 40
            and request.applicant_employee_id == profile_id
            and request.organization_unit_id == organization_id
            and request.title == command.title
            and request.purpose == command.purpose
            and request.needed_by_date == command.needed_by_date
            and request.currency == command.currency.value
            and request.total_amount == calculated.total_amount
        )

    @staticmethod
    def _items_match(
        items: Sequence[ProcurementRequestItem],
        *,
        request_id: uuid.UUID,
        command: SubmitProcurementRequestCommand,
        subtotals: Sequence[Decimal],
    ) -> bool:
        if len(items) != len(command.items) or not 1 <= len(items) <= 50:
            return False
        return all(
            item.request_id == request_id
            and item.line_number == line_number
            and item.category_code is expected.category_code
            and item.item_name == expected.item_name
            and item.specification == expected.specification
            and item.quantity == expected.quantity
            and item.unit == expected.unit
            and item.estimated_unit_price == expected.estimated_unit_price
            and item.subtotal == subtotals[line_number - 1]
            for line_number, (item, expected) in enumerate(
                zip(items, command.items, strict=True), start=1
            )
        )

    @staticmethod
    def _instance_matches(
        instance: ApprovalInstance,
        *,
        actor_user_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> bool:
        return (
            instance.process_key == PROCUREMENT_REQUEST_V1.process_key
            and instance.process_version == PROCUREMENT_REQUEST_V1.version
            and instance.subject_type == SUBJECT_TYPE
            and instance.applicant_user_id == actor_user_id
            and instance.organization_unit_id == organization_id
        )

    @staticmethod
    def _tasks_match(
        tasks: Sequence[ApprovalTask],
        *,
        instance_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> bool:
        if len(tasks) != 2:
            return False
        manager, procurement = tasks
        manager_step, procurement_step = PROCUREMENT_REQUEST_V1.steps
        return (
            manager.instance_id == instance_id
            and manager.sequence == manager_step.sequence
            and manager.step_key == manager_step.key
            and manager.step_label == manager_step.label
            and manager.assignment_kind is AssignmentKind.USER
            and manager.assigned_user_id is not None
            and manager.required_capability is None
            and manager.scope_organization_unit_id is None
            and procurement.instance_id == instance_id
            and procurement.sequence == procurement_step.sequence
            and procurement.step_key == procurement_step.key
            and procurement.step_label == procurement_step.label
            and procurement.assignment_kind is AssignmentKind.CAPABILITY
            and procurement.assigned_user_id is None
            and procurement.required_capability
            == Capability.PROCUREMENT_FINAL_REVIEW.value
            and procurement.scope_organization_unit_id == organization_id
        )

    @staticmethod
    def _audit_matches(
        audit: SecurityAuditEvent | None,
        *,
        actor_user_id: uuid.UUID,
        operation_id: uuid.UUID,
        request: ProcurementRequest,
        instance: ApprovalInstance,
        organization_id: uuid.UUID,
        count_bucket: str,
        total_bucket: str,
    ) -> bool:
        return audit is not None and (
            audit.event_name == "procurement_request_submitted"
            and audit.actor_user_id == actor_user_id
            and audit.target_type == RESULT_RESOURCE_TYPE
            and audit.target_id == request.id
            and audit.operation_id == operation_id
            and audit.outcome == "succeeded"
            and audit.summary
            == {
                "procurement_request_id": str(request.id),
                "approval_instance_id": str(instance.id),
                "organization_unit_id": str(organization_id),
                "item_count_bucket": count_bucket,
                "amount_bucket": total_bucket,
            }
        )

    @staticmethod
    def _event_matches(
        event: ProductEvent | None,
        *,
        event_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        organization_id: uuid.UUID,
        count_bucket: str,
        total_bucket: str,
    ) -> bool:
        return event is not None and (
            event.event_id == event_id
            and event.event_name == "procurement_request_submitted"
            and event.module_key == "procurement"
            and event.actor_user_id == actor_user_id
            and event.role_snapshot in {"employee", "hr", "admin"}
            and event.organization_unit_id == organization_id
            and event.outcome == "succeeded"
            and event.duration_ms is None
            and event.dimensions
            in (
                {
                    "stage": "submission",
                    "channel": "manual",
                    "item_count_bucket": count_bucket,
                    "amount_bucket": total_bucket,
                },
                {
                    "stage": "submission",
                    "channel": "ai_confirmation",
                    "item_count_bucket": count_bucket,
                    "amount_bucket": total_bucket,
                },
            )
        )


__all__ = [
    "PROCUREMENT_REQUEST_V1",
    "ProcurementApprovalAccess",
    "ProcurementItemCommand",
    "ProcurementRequestResult",
    "ProcurementService",
    "ProcurementSubmitPreflight",
    "ProcurementSubjectAdapter",
    "SubmitProcurementRequestCommand",
    "amount_bucket",
    "canonical_submission_hash",
    "item_count_bucket",
    "procurement_display_status",
]
