from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import importlib
import os
from threading import Barrier, Event
import uuid

import pytest
from sqlalchemy import create_engine, delete, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from policy_api.approvals.enums import (
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
from policy_api.approvals.repository import ApprovalRepository
from policy_api.approvals.service import ApprovalEngine
from policy_api.database import assert_test_database_url
from policy_api.hr.models import EmployeeProfile
from policy_api.models import User, UserRole
from policy_api.procurement.enums import (
    ProcurementCategoryCode,
    ProcurementCurrencyCode,
)
from policy_api.procurement.models import (
    ProcurementCommandOperation,
    ProcurementRequest,
    ProcurementRequestItem,
)
from policy_api.procurement.observability import ProcurementObservability
from policy_api.procurement.repository import ProcurementRepository
from policy_api.procurement.service import (
    ProcurementItemCommand,
    ProcurementService,
    SubmitProcurementRequestCommand,
)
from policy_api.tools.errors import ToolError
from policy_api.workbench.audit import SecurityAuditEvent
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityResolver,
    OrganizationUnit,
    ScopeKind,
)
from policy_api.workbench.events import ProductEvent, ProductEventEmitter


TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://policy_test@127.0.0.1:55434/"
    "procurement_approval_task2_test",
)
NOW = datetime(2026, 8, 24, 8, 30, tzinfo=timezone.utc)


def _task8_api():
    try:
        runtime_module = importlib.import_module("policy_api.approvals.runtime")
        adapter_module = importlib.import_module("policy_api.approvals.subject_adapter")
        procurement_module = importlib.import_module("policy_api.procurement.service")
        return (
            runtime_module.ApprovalRuntime,
            adapter_module.SubjectAdapterRegistry,
            procurement_module.ProcurementApprovalAccess,
            procurement_module.ProcurementSubjectAdapter,
        )
    except (ModuleNotFoundError, AttributeError) as error:
        pytest.fail(f"Task 8 workflow API is missing: {error}")


class SynchronizedLockRepository(ApprovalRepository):
    def __init__(self, barrier: Barrier) -> None:
        self._barrier = barrier

    def lock_instance(self, db: Session, instance_id: uuid.UUID):
        self._barrier.wait(timeout=5)
        return super().lock_instance(db, instance_id)


@dataclass(slots=True)
class WorkflowFixture:
    sessions: sessionmaker[Session]
    engine: object
    prefix: str
    applicant_id: uuid.UUID
    applicant_employee_id: uuid.UUID
    other_applicant_id: uuid.UUID
    manager_id: uuid.UUID
    reviewer_id: uuid.UUID
    reviewer_grant_id: uuid.UUID
    wrong_scope_reviewer_id: uuid.UUID
    nonassigned_manager_id: uuid.UUID
    admin_id: uuid.UUID
    hr_id: uuid.UUID
    organization_id: uuid.UUID
    root_organization_id: uuid.UUID
    user_ids: set[uuid.UUID] = field(default_factory=set)

    def actor(self, db: Session, user_id: uuid.UUID | None = None) -> User:
        actor = db.get(User, user_id or self.applicant_id)
        assert actor is not None
        return actor

    def procurement_service(
        self,
        *,
        approval_engine: ApprovalEngine | None = None,
        observability: ProcurementObservability | None = None,
    ) -> ProcurementService:
        return ProcurementService(
            approval_engine=approval_engine or ApprovalEngine(),
            now_factory=lambda: NOW,
            request_number_factory=lambda: f"PR-{uuid.uuid4().hex.upper()}",
            observability=observability,
        )

    def approval_runtime(
        self,
        *,
        approval_engine: ApprovalEngine | None = None,
        repository: ProcurementRepository | None = None,
        capability_resolver: CapabilityResolver | None = None,
        observability: ProcurementObservability | None = None,
    ):
        ApprovalRuntime, Registry, Access, Adapter = _task8_api()
        repository = repository or ProcurementRepository()
        return ApprovalRuntime(
            engine=approval_engine or ApprovalEngine(),
            registry=Registry((Adapter(repository=repository),)),
            access=Access(
                repository=repository,
                capability_resolver=capability_resolver or CapabilityResolver(),
            ),
            now_factory=lambda: NOW,
            transition_observer=(
                observability.stage_transition
                if observability is not None else None
            ),
        )


@pytest.fixture()
def workflow() -> WorkflowFixture:
    assert_test_database_url(TEST_DATABASE_URL)
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as connection:
        exists = connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'procurement_requests')"
            )
        )
        if not exists:
            pytest.fail("procurement migration head is required")
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    prefix = f"P8-{uuid.uuid4().hex[:12]}"
    root = OrganizationUnit(
        id=uuid.uuid4(), code=f"{prefix}-ROOT", name="Task 8 Root", is_active=True
    )
    unit = OrganizationUnit(
        id=uuid.uuid4(), code=f"{prefix}-UNIT", name="Task 8 Unit",
        parent_id=root.id, is_active=True,
    )
    other_unit = OrganizationUnit(
        id=uuid.uuid4(), code=f"{prefix}-OTHER", name="Other Unit", is_active=True
    )

    def user(label: str, role: UserRole = UserRole.EMPLOYEE) -> User:
        return User(
            id=uuid.uuid4(), username=f"{prefix}-{label}", password_hash="hash",
            role=role, is_active=True,
        )

    applicant = user("applicant")
    other_applicant = user("other-applicant")
    manager = user("manager")
    reviewer = user("reviewer")
    wrong_scope_reviewer = user("wrong-scope-reviewer")
    nonassigned_manager = user("nonassigned-manager")
    admin = user("admin", UserRole.ADMIN)
    hr = user("hr", UserRole.HR)
    manager_profile = EmployeeProfile(
        id=uuid.uuid4(), user_id=manager.id, employee_number=f"{prefix}-MGR",
        display_name="Manager", organization_unit_id=unit.id,
        manager_employee_id=None, hire_date=date(2020, 1, 1), is_active=True,
    )
    reviewer_profile = EmployeeProfile(
        id=uuid.uuid4(), user_id=reviewer.id, employee_number=f"{prefix}-REV",
        display_name="Reviewer", organization_unit_id=unit.id,
        manager_employee_id=manager_profile.id, hire_date=date(2022, 1, 1),
        is_active=True,
    )
    applicant_profile = EmployeeProfile(
        id=uuid.uuid4(), user_id=applicant.id, employee_number=f"{prefix}-APP",
        display_name="Applicant", organization_unit_id=unit.id,
        manager_employee_id=manager_profile.id, hire_date=date(2024, 1, 1),
        is_active=True,
    )
    other_profile = EmployeeProfile(
        id=uuid.uuid4(), user_id=other_applicant.id,
        employee_number=f"{prefix}-OTHER-APP", display_name="Other Applicant",
        organization_unit_id=unit.id, manager_employee_id=manager_profile.id,
        hire_date=date(2024, 1, 1), is_active=True,
    )
    grants = [
        CapabilityGrant(
            id=uuid.uuid4(), user_id=manager.id,
            capability=Capability.PROCUREMENT_DEPARTMENT_REVIEW.value,
            scope_kind=ScopeKind.UNIT_SUBTREE.value,
            organization_unit_id=root.id, is_active=True,
        ),
        CapabilityGrant(
            id=uuid.uuid4(), user_id=nonassigned_manager.id,
            capability=Capability.PROCUREMENT_DEPARTMENT_REVIEW.value,
            scope_kind=ScopeKind.UNIT_SUBTREE.value,
            organization_unit_id=root.id, is_active=True,
        ),
        CapabilityGrant(
            id=uuid.uuid4(), user_id=reviewer.id,
            capability=Capability.PROCUREMENT_FINAL_REVIEW.value,
            scope_kind=ScopeKind.UNIT_SUBTREE.value,
            organization_unit_id=root.id, is_active=True,
        ),
        CapabilityGrant(
            id=uuid.uuid4(), user_id=wrong_scope_reviewer.id,
            capability=Capability.PROCUREMENT_FINAL_REVIEW.value,
            scope_kind=ScopeKind.UNIT_SUBTREE.value,
            organization_unit_id=other_unit.id, is_active=True,
        ),
    ]
    users = [
        applicant, other_applicant, manager, reviewer, wrong_scope_reviewer,
        nonassigned_manager, admin, hr,
    ]
    with sessions.begin() as db:
        db.add_all([root, other_unit])
        db.flush()
        db.add(unit)
        db.add_all(users)
        db.flush()
        db.add_all(
            [manager_profile, reviewer_profile, applicant_profile, other_profile]
        )
        db.flush()
        db.add_all(grants)
    fixture = WorkflowFixture(
        sessions=sessions,
        engine=engine,
        prefix=prefix,
        applicant_id=applicant.id,
        applicant_employee_id=applicant_profile.id,
        other_applicant_id=other_applicant.id,
        manager_id=manager.id,
        reviewer_id=reviewer.id,
        reviewer_grant_id=grants[2].id,
        wrong_scope_reviewer_id=wrong_scope_reviewer.id,
        nonassigned_manager_id=nonassigned_manager.id,
        admin_id=admin.id,
        hr_id=hr.id,
        organization_id=unit.id,
        root_organization_id=root.id,
        user_ids={item.id for item in users},
    )
    try:
        yield fixture
    finally:
        with sessions.begin() as db:
            user_ids = tuple(fixture.user_ids)
            instance_ids = tuple(
                db.scalars(
                    select(ApprovalInstance.id).where(
                        ApprovalInstance.organization_unit_id == fixture.organization_id
                    )
                )
            )
            request_ids = tuple(
                db.scalars(
                    select(ProcurementRequest.id).where(
                        ProcurementRequest.approval_instance_id.in_(instance_ids)
                    )
                )
            )
            db.execute(delete(ProductEvent).where(ProductEvent.actor_user_id.in_(user_ids)))
            db.execute(delete(SecurityAuditEvent).where(SecurityAuditEvent.actor_user_id.in_(user_ids)))
            db.execute(delete(ProcurementCommandOperation).where(ProcurementCommandOperation.actor_user_id.in_(user_ids)))
            if request_ids:
                db.execute(delete(ProcurementRequestItem).where(ProcurementRequestItem.request_id.in_(request_ids)))
                db.execute(delete(ProcurementRequest).where(ProcurementRequest.id.in_(request_ids)))
            if instance_ids:
                db.execute(delete(ApprovalDecision).where(ApprovalDecision.instance_id.in_(instance_ids)))
                db.execute(delete(ApprovalCommandOperation).where(ApprovalCommandOperation.instance_id.in_(instance_ids)))
                db.execute(delete(ApprovalTask).where(ApprovalTask.instance_id.in_(instance_ids)))
                db.execute(delete(ApprovalInstance).where(ApprovalInstance.id.in_(instance_ids)))
            db.execute(delete(CapabilityGrant).where(CapabilityGrant.user_id.in_(user_ids)))
            db.execute(delete(EmployeeProfile).where(EmployeeProfile.user_id.in_(user_ids)))
            db.execute(delete(User).where(User.id.in_(user_ids)))
            db.execute(delete(OrganizationUnit).where(OrganizationUnit.code.like(f"{prefix}%")))
        engine.dispose()


def command() -> SubmitProcurementRequestCommand:
    return SubmitProcurementRequestCommand(
        title="研发采购",
        purpose="研发环境升级",
        needed_by_date=date(2030, 2, 1),
        currency=ProcurementCurrencyCode.CNY,
        items=(
            ProcurementItemCommand(
                category_code=ProcurementCategoryCode.IT_EQUIPMENT,
                item_name="开发工作站", specification="64 GB",
                quantity=Decimal("2.00"), unit="台",
                estimated_unit_price=Decimal("12000.00"),
            ),
        ),
    )


def submit(workflow: WorkflowFixture):
    with workflow.sessions() as db:
        result = workflow.procurement_service().submit_procurement_request(
            db,
            actor=workflow.actor(db),
            client_operation_id=uuid.uuid4(),
            command=command(),
            request_id="task8-workflow",
        )
        return result


def _error_code(callable_) -> str:
    with pytest.raises(ToolError) as captured:
        callable_()
    return captured.value.code


class FailAtProductEventEmitter(ProductEventEmitter):
    def __init__(self, fail_at: int) -> None:
        self._fail_at = fail_at
        self._calls = 0

    def append(self, db: Session, event_input):
        self._calls += 1
        if self._calls == self._fail_at:
            raise RuntimeError(f"product_event_failure_{self._fail_at}")
        return super().append(db, event_input)


class FailingTransitionObserver:
    def stage_transition(self, *_args, **_kwargs) -> None:
        raise RuntimeError("transition_observer_failure")


def _workflow_atomic_snapshot(
    workflow: WorkflowFixture,
    instance_id: uuid.UUID,
) -> tuple[object, ...]:
    with workflow.sessions() as db:
        instance = db.get(ApprovalInstance, instance_id)
        assert instance is not None
        tasks = tuple(
            (
                task.id,
                task.status.value,
                task.activated_at,
                task.completed_at,
            )
            for task in db.scalars(
                select(ApprovalTask)
                .where(ApprovalTask.instance_id == instance_id)
                .order_by(ApprovalTask.sequence)
            )
        )
        return (
            instance.status.value,
            instance.current_step_key,
            instance.completed_at,
            tasks,
            db.scalar(
                select(func.count())
                .select_from(ApprovalCommandOperation)
                .where(ApprovalCommandOperation.instance_id == instance_id)
            ),
            db.scalar(
                select(func.count())
                .select_from(ApprovalDecision)
                .where(ApprovalDecision.instance_id == instance_id)
            ),
            db.scalar(
                select(func.count())
                .select_from(SecurityAuditEvent)
                .where(SecurityAuditEvent.actor_user_id.in_(tuple(workflow.user_ids)))
            ),
            db.scalar(
                select(func.count())
                .select_from(ProductEvent)
                .where(ProductEvent.actor_user_id.in_(tuple(workflow.user_ids)))
            ),
        )


@pytest.mark.parametrize(
    ("transition_kind", "failure_point"),
    (
        *(("withdraw", point) for point in ("observer", "audit", "first_event")),
        *(("approve", point) for point in ("observer", "audit", "first_event")),
        *(("reject", point) for point in ("observer", "audit", "first_event")),
        *(("final_approve", point) for point in (
            "observer", "audit", "first_event", "completed_event",
        )),
    ),
)
def test_lifecycle_evidence_failure_rolls_back_complete_transition_transaction(
    workflow: WorkflowFixture,
    monkeypatch: pytest.MonkeyPatch,
    transition_kind: str,
    failure_point: str,
) -> None:
    submitted = submit(workflow)
    first_task_id, second_task_id = [task.id for task in submitted.tasks]
    if transition_kind == "final_approve":
        with workflow.sessions() as db:
            workflow.approval_runtime().approve_task(
                db,
                actor=workflow.actor(db, workflow.manager_id),
                task_id=first_task_id,
                client_operation_id=uuid.uuid4(),
                comment="department complete",
            )
    before = _workflow_atomic_snapshot(workflow, submitted.instance.id)

    if failure_point == "observer":
        observability = FailingTransitionObserver()
    else:
        fail_at = 2 if failure_point == "completed_event" else 1
        emitter = (
            FailAtProductEventEmitter(fail_at)
            if failure_point in {"first_event", "completed_event"}
            else ProductEventEmitter()
        )
        observability = ProcurementObservability(
            ProcurementRepository(),
            emitter,
        )
        if failure_point == "audit":
            def fail_audit(*_args, **_kwargs):
                raise RuntimeError("security_audit_failure")

            monkeypatch.setattr(
                "policy_api.procurement.observability.append_security_audit",
                fail_audit,
            )

    with workflow.sessions() as db, pytest.raises(RuntimeError):
        if transition_kind == "withdraw":
            workflow.procurement_service(
                observability=observability,  # type: ignore[arg-type]
            ).withdraw_request(
                db,
                actor=workflow.actor(db),
                request_id=submitted.request.id,
                client_operation_id=uuid.uuid4(),
                audit_request_id=f"atomic-{transition_kind}-{failure_point}",
            )
        else:
            actor_id = (
                workflow.reviewer_id
                if transition_kind == "final_approve"
                else workflow.manager_id
            )
            task_id = (
                second_task_id
                if transition_kind == "final_approve"
                else first_task_id
            )
            runtime = workflow.approval_runtime(
                observability=observability,  # type: ignore[arg-type]
            )
            if transition_kind == "reject":
                runtime.reject_task(
                    db,
                    actor=workflow.actor(db, actor_id),
                    task_id=task_id,
                    client_operation_id=uuid.uuid4(),
                    reason="reject for atomicity",
                    request_id=f"atomic-{transition_kind}-{failure_point}",
                )
            else:
                runtime.approve_task(
                    db,
                    actor=workflow.actor(db, actor_id),
                    task_id=task_id,
                    client_operation_id=uuid.uuid4(),
                    comment="approve for atomicity",
                    request_id=f"atomic-{transition_kind}-{failure_point}",
                )

    assert _workflow_atomic_snapshot(workflow, submitted.instance.id) == before


def test_owner_list_get_safe_404_status_timeline_and_copy_is_read_only(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    missing_id = uuid.uuid4()
    with workflow.sessions() as db:
        service = workflow.procurement_service()
        owner = workflow.actor(db)
        summaries = service.list_my_requests(db, actor=owner)
        detail = service.get_my_request(db, actor=owner, request_id=submitted.request.id)
        before = (
            db.scalar(select(func.count()).select_from(ProcurementRequest)),
            db.scalar(select(func.count()).select_from(ApprovalInstance)),
            db.scalar(select(func.count()).select_from(ProcurementCommandOperation)),
        )
        copied_source = service.get_my_request(
            db, actor=owner, request_id=submitted.request.id
        )
        after = (
            db.scalar(select(func.count()).select_from(ProcurementRequest)),
            db.scalar(select(func.count()).select_from(ApprovalInstance)),
            db.scalar(select(func.count()).select_from(ProcurementCommandOperation)),
        )
        other = workflow.actor(db, workflow.other_applicant_id)
        foreign_code = _error_code(
            lambda: service.get_my_request(
                db, actor=other, request_id=submitted.request.id
            )
        )
        missing_code = _error_code(
            lambda: service.get_my_request(db, actor=other, request_id=missing_id)
        )

    assert len(summaries) == 1
    assert summaries[0].status == "pending_manager"
    assert summaries[0].request_number == submitted.request.request_number
    assert detail.summary == summaries[0]
    assert detail.purpose == "研发环境升级"
    assert detail.items[0].subtotal == Decimal("24000.00")
    assert [entry.kind for entry in detail.timeline] == ["submitted"]
    assert copied_source == detail
    assert before == after
    assert foreign_code == missing_code == "procurement_request_not_found"


def test_owner_list_uses_one_authorized_pair_query_without_per_row_reload(
    workflow: WorkflowFixture,
) -> None:
    submitted = [submit(workflow) for _ in range(3)]
    statements: list[str] = []

    def count_statement(*args) -> None:
        statements.append(args[2])

    event.listen(workflow.engine, "before_cursor_execute", count_statement)
    try:
        with workflow.sessions() as db:
            summaries = workflow.procurement_service().list_my_requests(
                db, actor=workflow.actor(db)
            )
    finally:
        event.remove(workflow.engine, "before_cursor_execute", count_statement)

    assert {summary.request_number for summary in summaries} == {
        result.request.request_number for result in submitted
    }
    assert len(statements) == 4


def test_approval_detail_uses_submitted_organization_after_employee_move_and_deactivation(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    manager_task = submitted.tasks[0]
    moved_unit = OrganizationUnit(
        id=uuid.uuid4(),
        code=f"{workflow.prefix}-MOVED",
        name="Moved Unit",
        parent_id=workflow.root_organization_id,
        is_active=True,
    )
    with workflow.sessions() as db:
        db.add(moved_unit)
        db.flush()
        profile = db.get(EmployeeProfile, workflow.applicant_employee_id)
        submitted_unit = db.get(OrganizationUnit, workflow.organization_id)
        assert profile is not None and submitted_unit is not None
        profile.organization_unit_id = moved_unit.id
        submitted_unit.is_active = False
        db.commit()

    statements: list[str] = []

    def capture_statement(*args) -> None:
        statements.append(args[2])

    event.listen(workflow.engine, "before_cursor_execute", capture_statement)
    try:
        with workflow.sessions() as db:
            instance = db.get(ApprovalInstance, manager_task.instance_id)
            assert instance is not None
            _Runtime, _Registry, _Access, Adapter = _task8_api()
            detail = Adapter(repository=ProcurementRepository()).detail(db, instance)
    finally:
        event.remove(workflow.engine, "before_cursor_execute", capture_statement)

    assert detail.organization.display_name == "Task 8 Unit"
    assert detail.organization.display_name != "Moved Unit"
    identity_statements = [
        statement.lower()
        for statement in statements
        if "employee_profiles" in statement.lower()
        and "organization_units" in statement.lower()
    ]
    assert len(identity_statements) == 1


def test_owner_list_fast_path_rejects_malformed_process_pair(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    with workflow.sessions() as db:
        instance = db.get(ApprovalInstance, submitted.instance.id)
        assert instance is not None
        instance.process_version += 1
        db.commit()

    with workflow.sessions() as db:
        assert _error_code(
            lambda: workflow.procurement_service().list_my_requests(
                db, actor=workflow.actor(db)
            )
        ) == "approval_subject_invalid"


def test_manager_requires_exact_assignment_capability_and_pending_state(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    manager_task, waiting_task = submitted.tasks
    with workflow.sessions() as db:
        runtime = workflow.approval_runtime()
        manager = workflow.actor(db, workflow.manager_id)
        nonassigned = workflow.actor(db, workflow.nonassigned_manager_id)
        admin = workflow.actor(db, workflow.admin_id)
        hr = workflow.actor(db, workflow.hr_id)

        assert [item.task_id for item in runtime.list_actor_tasks(db, actor=manager)] == [manager_task.id]
        assert runtime.list_actor_tasks(db, actor=nonassigned) == ()
        assert runtime.list_actor_tasks(db, actor=admin) == ()
        assert runtime.list_actor_tasks(db, actor=hr) == ()
        assert _error_code(
            lambda: runtime.get_actor_task(db, actor=nonassigned, task_id=manager_task.id)
        ) == "approval_task_not_found"
        assert _error_code(
            lambda: runtime.approve_task(
                db, actor=nonassigned, task_id=manager_task.id,
                client_operation_id=uuid.uuid4(), comment=None,
            )
        ) == "approval_task_not_assigned"
        assert _error_code(
            lambda: runtime.approve_task(
                db, actor=workflow.actor(db, workflow.reviewer_id),
                task_id=waiting_task.id, client_operation_id=uuid.uuid4(),
                comment=None,
            )
        ) == "approval_task_state_conflict"


def test_final_reviewer_pool_scope_terminal_state_and_admin_hr_no_bypass(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    manager_task, final_task = submitted.tasks
    with workflow.sessions() as db:
        runtime = workflow.approval_runtime()
        runtime.approve_task(
            db, actor=workflow.actor(db, workflow.manager_id),
            task_id=manager_task.id, client_operation_id=uuid.uuid4(), comment="同意",
        )
    with workflow.sessions() as db:
        runtime = workflow.approval_runtime()
        reviewer = workflow.actor(db, workflow.reviewer_id)
        assert [item.task_id for item in runtime.list_actor_tasks(db, actor=reviewer)] == [final_task.id]
        assert runtime.list_actor_tasks(
            db, actor=workflow.actor(db, workflow.wrong_scope_reviewer_id)
        ) == ()
        for actor_id in (workflow.admin_id, workflow.hr_id):
            assert _error_code(
                lambda actor_id=actor_id: runtime.approve_task(
                    db, actor=workflow.actor(db, actor_id), task_id=final_task.id,
                    client_operation_id=uuid.uuid4(), comment=None,
                )
            ) == "approval_capability_required"
        transition = runtime.approve_task(
            db, actor=reviewer, task_id=final_task.id,
            client_operation_id=uuid.uuid4(), comment="已复核",
        )
        assert transition.instance.status is ApprovalInstanceStatus.APPROVED
        assert _error_code(
            lambda: runtime.approve_task(
                db, actor=reviewer, task_id=final_task.id,
                client_operation_id=uuid.uuid4(), comment=None,
            )
        ) == "approval_instance_state_conflict"


def test_queue_then_grant_revoke_is_rechecked_inside_write_transaction(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    manager_task, final_task = submitted.tasks
    with workflow.sessions() as db:
        runtime = workflow.approval_runtime()
        runtime.approve_task(
            db, actor=workflow.actor(db, workflow.manager_id),
            task_id=manager_task.id, client_operation_id=uuid.uuid4(), comment=None,
        )
    with workflow.sessions() as db:
        runtime = workflow.approval_runtime()
        reviewer = workflow.actor(db, workflow.reviewer_id)
        assert [item.task_id for item in runtime.list_actor_tasks(db, actor=reviewer)] == [final_task.id]
        grant = db.get(CapabilityGrant, workflow.reviewer_grant_id)
        assert grant is not None
        grant.is_active = False
        db.commit()
    with workflow.sessions() as db:
        runtime = workflow.approval_runtime()
        assert _error_code(
            lambda: runtime.approve_task(
                db, actor=workflow.actor(db, workflow.reviewer_id),
                task_id=final_task.id, client_operation_id=uuid.uuid4(), comment=None,
            )
        ) == "approval_capability_required"
        assert db.scalar(
            select(ApprovalTask.status).where(ApprovalTask.id == final_task.id)
        ) is ApprovalTaskStatus.PENDING


def test_cached_reviewer_scope_is_refreshed_before_decision(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    manager_task, final_task = submitted.tasks
    with workflow.sessions() as db:
        workflow.approval_runtime().approve_task(
            db,
            actor=workflow.actor(db, workflow.manager_id),
            task_id=manager_task.id,
            client_operation_id=uuid.uuid4(),
            comment=None,
        )

    with workflow.sessions() as decision_db:
        runtime = workflow.approval_runtime()
        reviewer = workflow.actor(decision_db, workflow.reviewer_id)
        cached_grant = decision_db.get(
            CapabilityGrant, workflow.reviewer_grant_id
        )
        assert cached_grant is not None
        assert cached_grant.organization_unit_id == workflow.root_organization_id
        assert [
            item.task_id for item in runtime.list_actor_tasks(decision_db, actor=reviewer)
        ] == [final_task.id]

        with workflow.sessions() as scope_db:
            grant = scope_db.get(CapabilityGrant, workflow.reviewer_grant_id)
            wrong_scope_root = scope_db.scalar(
                select(CapabilityGrant.organization_unit_id).where(
                    CapabilityGrant.user_id == workflow.wrong_scope_reviewer_id
                )
            )
            assert grant is not None
            assert wrong_scope_root is not None
            grant.organization_unit_id = wrong_scope_root
            scope_db.commit()

        assert cached_grant.organization_unit_id == workflow.root_organization_id
        assert _error_code(
            lambda: runtime.approve_task(
                decision_db,
                actor=reviewer,
                task_id=final_task.id,
                client_operation_id=uuid.uuid4(),
                comment=None,
            )
        ) == "approval_scope_denied"
        assert decision_db.scalar(
            select(ApprovalTask.status).where(ApprovalTask.id == final_task.id)
        ) is ApprovalTaskStatus.PENDING


def test_cached_reviewer_scope_is_refreshed_for_list_and_safe_get(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    manager_task, final_task = submitted.tasks
    with workflow.sessions() as db:
        workflow.approval_runtime().approve_task(
            db,
            actor=workflow.actor(db, workflow.manager_id),
            task_id=manager_task.id,
            client_operation_id=uuid.uuid4(),
            comment=None,
        )

    with workflow.sessions() as visibility_db:
        runtime = workflow.approval_runtime()
        reviewer = workflow.actor(visibility_db, workflow.reviewer_id)
        cached_grant = visibility_db.get(
            CapabilityGrant, workflow.reviewer_grant_id
        )
        assert cached_grant is not None
        assert [
            item.task_id
            for item in runtime.list_actor_tasks(visibility_db, actor=reviewer)
        ] == [final_task.id]
        assert runtime.get_actor_task(
            visibility_db, actor=reviewer, task_id=final_task.id
        ).task.task_id == final_task.id

        with workflow.sessions() as scope_db:
            grant = scope_db.get(CapabilityGrant, workflow.reviewer_grant_id)
            wrong_scope_root = scope_db.scalar(
                select(CapabilityGrant.organization_unit_id).where(
                    CapabilityGrant.user_id == workflow.wrong_scope_reviewer_id
                )
            )
            assert grant is not None
            assert wrong_scope_root is not None
            grant.organization_unit_id = wrong_scope_root
            scope_db.commit()

        assert cached_grant.organization_unit_id == workflow.root_organization_id
        assert runtime.list_actor_tasks(visibility_db, actor=reviewer) == ()
        assert _error_code(
            lambda: runtime.get_actor_task(
                visibility_db, actor=reviewer, task_id=final_task.id
            )
        ) == "approval_task_not_found"


class CoordinatedCapabilityResolver(CapabilityResolver):
    def __init__(
        self,
        *,
        grant_locked: Event,
        release_approval: Event,
    ) -> None:
        self._grant_locked = grant_locked
        self._release_approval = release_approval

    def scope_for(self, db: Session, user: User, capability: Capability):
        if capability is Capability.PROCUREMENT_FINAL_REVIEW:
            self._grant_locked.set()
            assert self._release_approval.wait(timeout=5)
        return super().scope_for(db, user, capability)


def test_approval_fact_lock_makes_approval_first_then_revoke(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    manager_task, final_task = submitted.tasks
    with workflow.sessions() as db:
        workflow.approval_runtime().approve_task(
            db,
            actor=workflow.actor(db, workflow.manager_id),
            task_id=manager_task.id,
            client_operation_id=uuid.uuid4(),
            comment=None,
        )

    grant_locked = Event()
    release_approval = Event()
    approval_pid_ready = Event()
    revoker_pid_ready = Event()
    backend_pids: dict[str, int] = {}

    def approve() -> tuple[str, str]:
        with workflow.sessions() as db:
            backend_pids["approval"] = db.scalar(text("SELECT pg_backend_pid()"))
            approval_pid_ready.set()
            try:
                transition = workflow.approval_runtime(
                    capability_resolver=CoordinatedCapabilityResolver(
                        grant_locked=grant_locked,
                        release_approval=release_approval,
                    )
                ).approve_task(
                    db,
                    actor=workflow.actor(db, workflow.reviewer_id),
                    task_id=final_task.id,
                    client_operation_id=uuid.uuid4(),
                    comment=None,
                )
                return "ok", transition.instance.status.value
            except ToolError as error:
                db.rollback()
                return "error", error.code

    def revoke() -> str:
        with workflow.sessions() as db:
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
            backend_pids["revoker"] = db.scalar(text("SELECT pg_backend_pid()"))
            revoker_pid_ready.set()
            grant = db.get(CapabilityGrant, workflow.reviewer_grant_id)
            assert grant is not None
            assert grant_locked.wait(timeout=5)
            grant.is_active = False
            db.flush()
            db.commit()
            return "ok"

    with ThreadPoolExecutor(max_workers=2) as executor:
        approve_future = executor.submit(approve)
        revoke_future = executor.submit(revoke)
        try:
            assert approval_pid_ready.wait(timeout=5)
            assert revoker_pid_ready.wait(timeout=5)
            assert grant_locked.wait(timeout=5)
            blocked_by_approval = False
            with workflow.sessions() as observer_db:
                for _ in range(500):
                    wait_event_type, blockers = observer_db.execute(
                        text(
                            "SELECT wait_event_type, pg_blocking_pids(pid) "
                            "FROM pg_stat_activity WHERE pid = :pid"
                        ),
                        {"pid": backend_pids["revoker"]},
                    ).one()
                    if (
                        wait_event_type == "Lock"
                        and backend_pids["approval"] in blockers
                    ):
                        blocked_by_approval = True
                        break
            assert blocked_by_approval
            assert revoke_future.done() is False
        finally:
            release_approval.set()
        approve_result = approve_future.result(timeout=10)
        revoke_result = revoke_future.result(timeout=10)

    assert approve_result == ("ok", ApprovalInstanceStatus.APPROVED.value)
    assert revoke_result == "ok"
    with workflow.sessions() as db:
        grant = db.get(CapabilityGrant, workflow.reviewer_grant_id)
        instance = db.get(ApprovalInstance, submitted.instance.id)
        assert grant is not None
        assert instance is not None
        assert grant.is_active is False
        assert instance.status is ApprovalInstanceStatus.APPROVED
        assert db.scalar(
            select(func.count()).select_from(ApprovalDecision).where(
                ApprovalDecision.instance_id == instance.id,
                ApprovalDecision.task_id == final_task.id,
            )
        ) == 1
        final_operation = db.scalar(
            select(ApprovalCommandOperation).where(
                ApprovalCommandOperation.instance_id == instance.id,
                ApprovalCommandOperation.task_id == final_task.id,
            )
        )
        assert final_operation is not None
        assert final_operation.status is ApprovalCommandStatus.SUCCEEDED


def test_revoke_commit_first_makes_later_approval_fail_closed(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    manager_task, final_task = submitted.tasks
    with workflow.sessions() as db:
        workflow.approval_runtime().approve_task(
            db,
            actor=workflow.actor(db, workflow.manager_id),
            task_id=manager_task.id,
            client_operation_id=uuid.uuid4(),
            comment=None,
        )

    revocation_committed = Barrier(2)

    def approve() -> str:
        revocation_committed.wait(timeout=5)
        with workflow.sessions() as db:
            return _error_code(
                lambda: workflow.approval_runtime().approve_task(
                    db,
                    actor=workflow.actor(db, workflow.reviewer_id),
                    task_id=final_task.id,
                    client_operation_id=uuid.uuid4(),
                    comment=None,
                )
            )

    def revoke() -> None:
        with workflow.sessions() as db:
            grant = db.get(CapabilityGrant, workflow.reviewer_grant_id)
            assert grant is not None
            grant.is_active = False
            db.commit()
        revocation_committed.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        approve_future = executor.submit(approve)
        revoke_future = executor.submit(revoke)
        assert approve_future.result(timeout=10) == "approval_capability_required"
        revoke_future.result(timeout=10)

    with workflow.sessions() as db:
        assert db.scalar(
            select(ApprovalTask.status).where(ApprovalTask.id == final_task.id)
        ) is ApprovalTaskStatus.PENDING
        assert db.scalar(
            select(func.count()).select_from(ApprovalDecision).where(
                ApprovalDecision.instance_id == submitted.instance.id,
                ApprovalDecision.task_id == final_task.id,
            )
        ) == 0


def test_queue_then_manager_profile_revoke_fails_closed_without_mutation(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    manager_task, _ = submitted.tasks
    with workflow.sessions() as db:
        runtime = workflow.approval_runtime()
        manager = workflow.actor(db, workflow.manager_id)
        assert [item.task_id for item in runtime.list_actor_tasks(db, actor=manager)] == [
            manager_task.id
        ]
        manager_profile = db.scalar(
            select(EmployeeProfile).where(
                EmployeeProfile.user_id == workflow.manager_id
            )
        )
        assert manager_profile is not None
        manager_profile.is_active = False
        db.commit()

    with workflow.sessions() as db:
        request_before = db.get(ProcurementRequest, submitted.request.id)
        assert request_before is not None
        business_state_before = (
            request_before.title,
            request_before.purpose,
            request_before.total_amount,
            request_before.updated_at,
        )
        assert _error_code(
            lambda: workflow.approval_runtime().approve_task(
                db,
                actor=workflow.actor(db, workflow.manager_id),
                task_id=manager_task.id,
                client_operation_id=uuid.uuid4(),
                comment=None,
            )
        ) == "approval_capability_required"

        task = db.get(ApprovalTask, manager_task.id)
        instance = db.get(ApprovalInstance, submitted.instance.id)
        request_after = db.get(ProcurementRequest, submitted.request.id)
        assert task is not None
        assert instance is not None
        assert request_after is not None
        assert task.status is ApprovalTaskStatus.PENDING
        assert instance.status is ApprovalInstanceStatus.RUNNING
        assert instance.current_step_key == "department_manager_review"
        assert db.scalar(
            select(func.count()).select_from(ApprovalDecision).where(
                ApprovalDecision.instance_id == submitted.instance.id
            )
        ) == 0
        assert db.scalar(
            select(func.count()).select_from(ApprovalCommandOperation).where(
                ApprovalCommandOperation.instance_id == submitted.instance.id
            )
        ) == 0
        assert (
            request_after.title,
            request_after.purpose,
            request_after.total_amount,
            request_after.updated_at,
        ) == business_state_before


def test_reject_reason_is_required_and_each_step_rejects_terminally(
    workflow: WorkflowFixture,
) -> None:
    first = submit(workflow)
    manager_task, waiting_task = first.tasks
    with workflow.sessions() as db:
        runtime = workflow.approval_runtime()
        assert _error_code(
            lambda: runtime.reject_task(
                db, actor=workflow.actor(db, workflow.manager_id),
                task_id=manager_task.id, client_operation_id=uuid.uuid4(), reason="  ",
            )
        ) == "approval_decision_reason_required"
        rejected = runtime.reject_task(
            db, actor=workflow.actor(db, workflow.manager_id),
            task_id=manager_task.id, client_operation_id=uuid.uuid4(), reason="预算不足",
        )
        assert rejected.instance.status is ApprovalInstanceStatus.REJECTED
        assert [task.status for task in rejected.tasks] == [
            ApprovalTaskStatus.REJECTED,
            ApprovalTaskStatus.CANCELLED,
        ]

    second = submit(workflow)
    manager_task, final_task = second.tasks
    with workflow.sessions() as db:
        runtime = workflow.approval_runtime()
        runtime.approve_task(
            db, actor=workflow.actor(db, workflow.manager_id),
            task_id=manager_task.id, client_operation_id=uuid.uuid4(), comment="部门同意",
        )
        rejected = runtime.reject_task(
            db, actor=workflow.actor(db, workflow.reviewer_id),
            task_id=final_task.id, client_operation_id=uuid.uuid4(), reason="采购规则不符",
        )
        assert rejected.instance.status is ApprovalInstanceStatus.REJECTED
        decisions = tuple(
            db.scalars(
                select(ApprovalDecision)
                .join(ApprovalTask, ApprovalTask.id == ApprovalDecision.task_id)
                .where(ApprovalDecision.instance_id == second.instance.id)
                .order_by(ApprovalTask.sequence)
            )
        )
        assert [decision.action.value for decision in decisions] == ["approve", "reject"]
        detail = workflow.procurement_service().get_my_request(
            db, actor=workflow.actor(db), request_id=second.request.id
        )
        assert detail.summary.status == "rejected"
        assert [entry.action for entry in detail.timeline if entry.kind == "decision"] == [
            "approve", "reject"
        ]


@pytest.mark.parametrize("after_manager_approval", [False, True])
def test_running_both_stages_withdraw_replays_and_cannot_revive(
    workflow: WorkflowFixture, after_manager_approval: bool,
) -> None:
    submitted = submit(workflow)
    manager_task, final_task = submitted.tasks
    if after_manager_approval:
        with workflow.sessions() as db:
            workflow.approval_runtime().approve_task(
                db, actor=workflow.actor(db, workflow.manager_id),
                task_id=manager_task.id, client_operation_id=uuid.uuid4(),
                comment="部门同意",
            )
    operation_id = uuid.uuid4()
    with workflow.sessions() as db:
        service = workflow.procurement_service()
        owner = workflow.actor(db)
        first = service.withdraw_request(
            db, actor=owner, request_id=submitted.request.id,
            client_operation_id=operation_id,
        )
        replay = service.withdraw_request(
            db, actor=owner, request_id=submitted.request.id,
            client_operation_id=operation_id,
        )
        assert first.replayed is False
        assert replay.replayed is True
        assert first.instance.status is ApprovalInstanceStatus.CANCELLED
        expected = (
            [ApprovalTaskStatus.APPROVED, ApprovalTaskStatus.CANCELLED]
            if after_manager_approval
            else [ApprovalTaskStatus.CANCELLED, ApprovalTaskStatus.CANCELLED]
        )
        assert [task.status for task in replay.tasks] == expected
        assert _error_code(
            lambda: service.withdraw_request(
                db, actor=owner, request_id=submitted.request.id,
                client_operation_id=uuid.uuid4(),
            )
        ) == "approval_instance_state_conflict"
        assert _error_code(
            lambda: workflow.approval_runtime().approve_task(
                db,
                actor=workflow.actor(
                    db,
                    workflow.reviewer_id if after_manager_approval else workflow.manager_id,
                ),
                task_id=final_task.id if after_manager_approval else manager_task.id,
                client_operation_id=uuid.uuid4(), comment=None,
            )
        ) == "approval_instance_state_conflict"
        detail = service.get_my_request(db, actor=owner, request_id=submitted.request.id)
        assert detail.summary.status == "cancelled"
        assert detail.timeline[-1].kind == "withdrawn"
        assert len(tuple(db.scalars(
            select(ApprovalDecision).where(ApprovalDecision.instance_id == submitted.instance.id)
        ))) == (1 if after_manager_approval else 0)


def test_completed_request_cannot_be_withdrawn(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    manager_task, final_task = submitted.tasks
    with workflow.sessions() as db:
        runtime = workflow.approval_runtime()
        runtime.approve_task(
            db, actor=workflow.actor(db, workflow.manager_id), task_id=manager_task.id,
            client_operation_id=uuid.uuid4(), comment=None,
        )
        runtime.approve_task(
            db, actor=workflow.actor(db, workflow.reviewer_id), task_id=final_task.id,
            client_operation_id=uuid.uuid4(), comment=None,
        )
        assert _error_code(
            lambda: workflow.procurement_service().withdraw_request(
                db, actor=workflow.actor(db), request_id=submitted.request.id,
                client_operation_id=uuid.uuid4(),
            )
        ) == "approval_instance_state_conflict"


def test_withdraw_vs_final_approve_real_postgresql_has_one_terminal_winner(
    workflow: WorkflowFixture,
) -> None:
    submitted = submit(workflow)
    manager_task, final_task = submitted.tasks
    with workflow.sessions() as db:
        workflow.approval_runtime().approve_task(
            db, actor=workflow.actor(db, workflow.manager_id), task_id=manager_task.id,
            client_operation_id=uuid.uuid4(), comment=None,
        )

    barrier = Barrier(2)

    def approve():
        with workflow.sessions() as db:
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            try:
                result = workflow.approval_runtime(
                    approval_engine=ApprovalEngine(SynchronizedLockRepository(barrier))
                ).approve_task(
                    db, actor=workflow.actor(db, workflow.reviewer_id),
                    task_id=final_task.id, client_operation_id=uuid.uuid4(), comment=None,
                )
                return "ok", result.instance.status.value
            except ToolError as error:
                db.rollback()
                return "error", error.code

    def withdraw():
        with workflow.sessions() as db:
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            try:
                result = workflow.procurement_service(
                    approval_engine=ApprovalEngine(SynchronizedLockRepository(barrier))
                ).withdraw_request(
                    db, actor=workflow.actor(db), request_id=submitted.request.id,
                    client_operation_id=uuid.uuid4(),
                )
                return "ok", result.instance.status.value
            except ToolError as error:
                db.rollback()
                return "error", error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result(timeout=10)
            for future in (executor.submit(approve), executor.submit(withdraw))
        ]
    assert sorted(result[0] for result in results) == ["error", "ok"]
    assert [value for outcome, value in results if outcome == "error"] == [
        "approval_instance_state_conflict"
    ]
    with workflow.sessions() as db:
        instance = db.get(ApprovalInstance, submitted.instance.id)
        assert instance is not None
        assert instance.status in {
            ApprovalInstanceStatus.APPROVED,
            ApprovalInstanceStatus.CANCELLED,
        }
        assert instance.current_step_key is None
        assert not tuple(
            db.scalars(
                select(ApprovalTask).where(
                    ApprovalTask.instance_id == instance.id,
                    ApprovalTask.status.in_(
                        (ApprovalTaskStatus.PENDING, ApprovalTaskStatus.WAITING)
                    ),
                )
            )
        )
        operations = tuple(
            db.scalars(
                select(ApprovalCommandOperation).where(
                    ApprovalCommandOperation.instance_id == instance.id
                )
            )
        )
        assert len(operations) == 2
        assert all(
            operation.status is ApprovalCommandStatus.SUCCEEDED
            for operation in operations
        )
