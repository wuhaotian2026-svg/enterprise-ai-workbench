from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from threading import Barrier, Event
import uuid

import pytest
from sqlalchemy import create_engine, delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from policy_api.assistant_drafts.models import AssistantFlowDraft
from policy_api.approvals.models import (
    ApprovalCommandOperation,
    ApprovalDecision,
    ApprovalInstance,
    ApprovalTask,
)
from policy_api.approvals.enums import (
    ApprovalInstanceStatus,
    ApprovalTaskStatus,
    AssignmentKind,
)
from policy_api.approvals.service import ApprovalEngine
from policy_api.database import assert_test_database_url
from policy_api.hr.models import EmployeeProfile
from policy_api.models import User, UserRole
from policy_api.procurement.enums import (
    ProcurementCategoryCode,
    ProcurementCommandOperationStatus,
    ProcurementCurrencyCode,
)
from policy_api.procurement.models import (
    AssistantConversation,
    AssistantTurn,
    ProcurementCommandOperation,
    ProcurementRequest,
    ProcurementRequestItem,
)
from policy_api.slot_extraction.models import SlotExtractionOperation
from policy_api.procurement.repository import ProcurementRepository
from policy_api.procurement.service import (
    ProcurementItemCommand,
    ProcurementService,
    SubmitProcurementRequestCommand,
    canonical_submission_hash,
)
from policy_api.tools.errors import ToolError
from policy_api.workbench.audit import SecurityAuditEvent, append_security_audit
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    OrganizationUnit,
    ScopeKind,
)
from policy_api.workbench.events import ProductEvent, ProductEventEmitter


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
NOW = datetime(2026, 8, 24, 2, 30, tzinfo=timezone.utc)
COUNT_MODELS = (
    ProcurementCommandOperation,
    ProcurementRequest,
    ProcurementRequestItem,
    ApprovalInstance,
    ApprovalTask,
    ApprovalDecision,
    SecurityAuditEvent,
    ProductEvent,
)


@dataclass(slots=True)
class ProcurementFixture:
    sessions: sessionmaker[Session]
    prefix: str
    applicant_id: uuid.UUID
    applicant_employee_id: uuid.UUID
    manager_id: uuid.UUID
    manager_employee_id: uuid.UUID
    organization_id: uuid.UUID
    user_ids: set[uuid.UUID] = field(default_factory=set)

    def actor(self, db: Session, user_id: uuid.UUID | None = None) -> User:
        actor = db.get(User, user_id or self.applicant_id)
        assert actor is not None
        return actor

    def service(self, **overrides: object) -> ProcurementService:
        dependencies: dict[str, object] = {
            "now_factory": lambda: NOW,
            "request_number_factory": lambda: f"PR-{uuid.uuid4().hex.upper()}",
        }
        dependencies.update(overrides)
        return ProcurementService(**dependencies)

    def add_applicant(self) -> uuid.UUID:
        suffix = uuid.uuid4().hex
        user = User(
            id=uuid.uuid4(),
            username=f"{self.prefix}-applicant-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        with self.sessions.begin() as db:
            db.add(user)
            db.flush()
            db.add(
                EmployeeProfile(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    employee_number=f"{self.prefix}-E-{suffix[:16]}",
                    display_name="Second Applicant",
                    organization_unit_id=self.organization_id,
                    manager_employee_id=self.manager_employee_id,
                    hire_date=date(2024, 1, 1),
                    is_active=True,
                )
            )
        self.user_ids.add(user.id)
        return user.id


@pytest.fixture()
def procurement_fixture() -> ProcurementFixture:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for procurement transaction tests")
    assert_test_database_url(TEST_DATABASE_URL)
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as connection:
        exists = connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'procurement_command_operations')"
            )
        )
        if not exists:
            pytest.fail("procurement migration head is required")
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    prefix = f"P7-{uuid.uuid4().hex[:12]}"
    applicant = User(
        id=uuid.uuid4(), username=f"{prefix}-applicant", password_hash="hash",
        role=UserRole.EMPLOYEE, is_active=True,
    )
    manager = User(
        id=uuid.uuid4(), username=f"{prefix}-manager", password_hash="hash",
        role=UserRole.EMPLOYEE, is_active=True,
    )
    unit = OrganizationUnit(
        id=uuid.uuid4(), code=prefix, name="Task 7 Unit", is_active=True
    )
    manager_profile = EmployeeProfile(
        id=uuid.uuid4(), user_id=manager.id, employee_number=f"{prefix}-MGR",
        display_name="Manager", organization_unit_id=unit.id,
        manager_employee_id=None, hire_date=date(2020, 1, 1), is_active=True,
    )
    applicant_profile = EmployeeProfile(
        id=uuid.uuid4(), user_id=applicant.id, employee_number=f"{prefix}-APP",
        display_name="Applicant", organization_unit_id=unit.id,
        manager_employee_id=manager_profile.id, hire_date=date(2024, 1, 1),
        is_active=True,
    )
    grant = CapabilityGrant(
        id=uuid.uuid4(), user_id=manager.id,
        capability=Capability.PROCUREMENT_DEPARTMENT_REVIEW.value,
        scope_kind=ScopeKind.UNIT_SUBTREE.value,
        organization_unit_id=unit.id, is_active=True,
    )
    with sessions.begin() as db:
        db.add_all([applicant, manager, unit])
        db.flush()
        db.add_all([manager_profile, applicant_profile])
        db.flush()
        db.add(grant)
    fixture = ProcurementFixture(
        sessions=sessions,
        prefix=prefix,
        applicant_id=applicant.id,
        applicant_employee_id=applicant_profile.id,
        manager_id=manager.id,
        manager_employee_id=manager_profile.id,
        organization_id=unit.id,
        user_ids={applicant.id, manager.id},
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
                        or_(
                            ProcurementRequest.organization_unit_id
                            == fixture.organization_id,
                            ProcurementRequest.approval_instance_id.in_(instance_ids),
                        )
                    )
                )
            )
            db.execute(delete(ProductEvent).where(ProductEvent.actor_user_id.in_(user_ids)))
            db.execute(delete(SecurityAuditEvent).where(SecurityAuditEvent.actor_user_id.in_(user_ids)))
            db.execute(delete(ProcurementCommandOperation).where(ProcurementCommandOperation.actor_user_id.in_(user_ids)))
            db.execute(delete(AssistantTurn).where(AssistantTurn.owner_user_id.in_(user_ids)))
            db.execute(delete(AssistantConversation).where(AssistantConversation.owner_user_id.in_(user_ids)))
            db.execute(delete(AssistantFlowDraft).where(AssistantFlowDraft.owner_user_id.in_(user_ids)))
            db.execute(delete(SlotExtractionOperation).where(SlotExtractionOperation.owner_user_id.in_(user_ids)))
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
            db.execute(
                delete(OrganizationUnit).where(
                    OrganizationUnit.code.like(f"{fixture.prefix}%")
                )
            )
            db.execute(delete(User).where(User.id.in_(user_ids)))
        engine.dispose()


def submission_command(*, title: str = "研发采购") -> SubmitProcurementRequestCommand:
    return SubmitProcurementRequestCommand(
        title=title,
        purpose="研发环境升级",
        needed_by_date=date(2030, 2, 1),
        currency=ProcurementCurrencyCode.CNY,
        items=(
            ProcurementItemCommand(
                category_code=ProcurementCategoryCode.IT_EQUIPMENT,
                item_name="开发工作站",
                specification="64 GB",
                quantity=Decimal("2.00"), unit="台",
                estimated_unit_price=Decimal("12000.00"),
            ),
            ProcurementItemCommand(
                category_code=ProcurementCategoryCode.SOFTWARE_SERVICE,
                item_name="开发许可",
                specification=None,
                quantity=Decimal("3"), unit="套",
                estimated_unit_price=Decimal("500.00"),
            ),
        ),
    )


def submit(
    fixture: ProcurementFixture,
    db: Session,
    *,
    operation_id: uuid.UUID | None = None,
    command: SubmitProcurementRequestCommand | None = None,
    actor_id: uuid.UUID | None = None,
    actor: User | None = None,
    service: ProcurementService | None = None,
    channel: str = "manual",
    commit: bool = True,
):
    return (service or fixture.service()).submit_procurement_request(
        db,
        actor=actor or fixture.actor(db, actor_id),
        client_operation_id=operation_id or uuid.uuid4(),
        command=command or submission_command(),
        request_id="trace-task7",
        channel=channel,
        commit=commit,
    )


def aggregate_counts(fixture: ProcurementFixture) -> tuple[int, ...]:
    with fixture.sessions() as db:
        user_ids = tuple(fixture.user_ids)
        request_ids = select(ProcurementRequest.id).where(
            ProcurementRequest.organization_unit_id == fixture.organization_id
        )
        instance_ids = select(ApprovalInstance.id).where(
            ApprovalInstance.organization_unit_id == fixture.organization_id
        )
        return (
            db.scalar(select(func.count()).select_from(ProcurementCommandOperation).where(ProcurementCommandOperation.actor_user_id.in_(user_ids))) or 0,
            db.scalar(select(func.count()).select_from(ProcurementRequest).where(ProcurementRequest.organization_unit_id == fixture.organization_id)) or 0,
            db.scalar(select(func.count()).select_from(ProcurementRequestItem).where(ProcurementRequestItem.request_id.in_(request_ids))) or 0,
            db.scalar(select(func.count()).select_from(ApprovalInstance).where(ApprovalInstance.id.in_(instance_ids))) or 0,
            db.scalar(select(func.count()).select_from(ApprovalTask).where(ApprovalTask.instance_id.in_(instance_ids))) or 0,
            db.scalar(select(func.count()).select_from(ApprovalDecision).where(ApprovalDecision.instance_id.in_(instance_ids))) or 0,
            db.scalar(select(func.count()).select_from(SecurityAuditEvent).where(SecurityAuditEvent.actor_user_id.in_(user_ids), SecurityAuditEvent.event_name == "procurement_request_submitted")) or 0,
            db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.actor_user_id.in_(user_ids), ProductEvent.event_name == "procurement_request_submitted")) or 0,
        )


class AllowApprovalDecision:
    def authorize(self, *, instance, task, actor, action) -> None:
        return None


class FailingItemRepository(ProcurementRepository):
    def add_request_item(self, db: Session, item: ProcurementRequestItem) -> None:
        super().add_request_item(db, item)
        db.flush()
        raise RuntimeError("item persist failed")


class FailingSecondTaskEngine(ApprovalEngine):
    def start_instance(self, *args, **kwargs):
        instance, tasks = super().start_instance(*args, **kwargs)
        assert len(tasks) == 2
        raise RuntimeError("second approval task failed")


class FailingEventEmitter(ProductEventEmitter):
    def append(self, db: Session, event):
        super().append(db, event)
        raise RuntimeError("product event failed")


def failing_audit(*args, **kwargs):
    append_security_audit(*args, **kwargs)
    raise RuntimeError("security audit failed")


@pytest.mark.parametrize(
    "service",
    [
        lambda f: f.service(repository=FailingItemRepository()),
        lambda f: f.service(approval_engine=FailingSecondTaskEngine()),
        lambda f: f.service(audit_appender=failing_audit),
        lambda f: f.service(product_event_emitter=FailingEventEmitter()),
    ],
    ids=["item", "second_task", "security_audit", "product_event"],
)
def test_dependency_failure_rolls_back_all_eight_resource_types(
    procurement_fixture: ProcurementFixture, service
) -> None:
    with procurement_fixture.sessions() as db:
        with pytest.raises(RuntimeError):
            submit(procurement_fixture, db, service=service(procurement_fixture))
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


class CorruptAtCompletionRepository(ProcurementRepository):
    def __init__(self, target: str) -> None:
        self.target = target
        self.request: ProcurementRequest | None = None
        self.item: ProcurementRequestItem | None = None

    def add_request(self, db: Session, request: ProcurementRequest) -> None:
        self.request = request
        super().add_request(db, request)

    def add_request_item(self, db: Session, item: ProcurementRequestItem) -> None:
        self.item = item
        super().add_request_item(db, item)

    def complete_operation(self, operation, request_id):
        super().complete_operation(operation, request_id)
        if self.target == "request_not_null":
            assert self.request is not None
            self.request.title = None  # type: ignore[assignment]
        else:
            assert self.item is not None
            self.item.quantity = Decimal("0")


@pytest.mark.parametrize("target", ["request_not_null", "item_check"])
def test_final_aggregate_flush_real_constraint_failure_rolls_back_everything(
    procurement_fixture: ProcurementFixture, target: str
) -> None:
    repository = CorruptAtCompletionRepository(target)
    with procurement_fixture.sessions() as db:
        with pytest.raises(Exception):
            submit(procurement_fixture, db, service=procurement_fixture.service(repository=repository))
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


def test_success_creates_exact_aggregate_and_closed_low_sensitivity_events(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    command = submission_command()
    with procurement_fixture.sessions() as db:
        result = submit(
            procurement_fixture,
            db,
            operation_id=operation_id,
            command=command,
        )
        assert result.replayed is False
        assert result.request.request_number.startswith("PR-")
        assert len(result.request.request_number) <= 40
        assert [item.line_number for item in result.items] == [1, 2]
        assert [item.subtotal for item in result.items] == [
            Decimal("24000.00"),
            Decimal("1500.00"),
        ]
        assert result.request.total_amount == Decimal("25500.00")
        assert result.instance.process_key == "procurement.request"
        assert result.instance.process_version == 1
        assert result.instance.subject_type == "procurement_request"
        assert [task.step_key for task in result.tasks] == ["department_manager_review", "procurement_review"]
        assert [task.status for task in result.tasks] == [
            ApprovalTaskStatus.PENDING,
            ApprovalTaskStatus.WAITING,
        ]
        assert [task.assignment_kind for task in result.tasks] == [
            AssignmentKind.USER,
            AssignmentKind.CAPABILITY,
        ]
        assert result.tasks[0].assigned_user_id == procurement_fixture.manager_id
        assert result.tasks[1].required_capability == Capability.PROCUREMENT_FINAL_REVIEW.value
        assert result.tasks[1].scope_organization_unit_id == procurement_fixture.organization_id
        assert result.operation.status is ProcurementCommandOperationStatus.SUCCEEDED
        assert result.operation.command_kind == "procurement.submit"
        assert result.operation.canonical_payload_hash == canonical_submission_hash(command)
        assert result.operation.result_resource_type == "procurement_request"
        assert result.operation.result_resource_id == result.request.id
        audit = db.scalar(select(SecurityAuditEvent).where(SecurityAuditEvent.actor_user_id == procurement_fixture.applicant_id))
        event = db.scalar(select(ProductEvent).where(ProductEvent.actor_user_id == procurement_fixture.applicant_id))
        assert audit is not None and event is not None
        assert audit.target_type == "procurement_request"
        assert audit.target_id == result.request.id
        assert audit.operation_id == operation_id
        assert audit.outcome == "succeeded"
        assert audit.request_id == "trace-task7"
        assert set(audit.summary) == {"procurement_request_id", "approval_instance_id", "organization_unit_id", "item_count_bucket", "amount_bucket"}
        assert event.module_key == "procurement"
        assert event.organization_unit_id == procurement_fixture.organization_id
        assert event.role_snapshot == "employee"
        assert event.request_id == "trace-task7"
        assert event.outcome == "succeeded"
        assert event.dimensions == {
            "stage": "submission",
            "channel": "manual",
            "item_count_bucket": "2_5",
            "amount_bucket": "10000_99999",
        }
        serialized = f"{audit.summary!r}{event.dimensions!r}"
        for secret in ("研发采购", "研发环境升级", "开发工作站", "64 GB", "25500.00"):
            assert secret not in serialized
    assert aggregate_counts(procurement_fixture) == (1, 1, 2, 1, 2, 0, 1, 1)


def test_exact_replay_returns_authoritative_identity_without_writes(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    with procurement_fixture.sessions() as db:
        first = submit(procurement_fixture, db, operation_id=operation_id)
        first_counts = aggregate_counts(procurement_fixture)
        replay = submit(procurement_fixture, db, operation_id=operation_id)
        assert replay.replayed is True
        assert replay.request.id == first.request.id
        assert replay.request.request_number == first.request.request_number
        assert replay.instance.id == first.instance.id
        assert [item.id for item in replay.items] == [item.id for item in first.items]
        assert [task.id for task in replay.tasks] == [task.id for task in first.tasks]
    assert aggregate_counts(procurement_fixture) == first_counts == (1, 1, 2, 1, 2, 0, 1, 1)


def test_exact_replay_ignores_non_authoritative_submission_channel(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    with procurement_fixture.sessions() as db:
        first = submit(
            procurement_fixture,
            db,
            operation_id=operation_id,
            channel="manual",
        )
        before = aggregate_counts(procurement_fixture)
        replay = submit(
            procurement_fixture,
            db,
            operation_id=operation_id,
            channel="ai_confirmation",
        )

    assert replay.replayed is True
    assert replay.request.id == first.request.id
    assert aggregate_counts(procurement_fixture) == before


def test_exact_replay_remains_available_after_needed_date_passes(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    command = submission_command()
    clock = {"now": NOW}
    service = procurement_fixture.service(now_factory=lambda: clock["now"])
    with procurement_fixture.sessions() as db:
        first = submit(
            procurement_fixture,
            db,
            operation_id=operation_id,
            command=command,
            service=service,
        )
    before = aggregate_counts(procurement_fixture)
    clock["now"] = datetime(2030, 2, 2, tzinfo=timezone.utc)

    with procurement_fixture.sessions() as db:
        replay = submit(
            procurement_fixture,
            db,
            operation_id=operation_id,
            command=command,
            service=service,
        )

    assert replay.replayed is True
    assert replay.request.id == first.request.id
    assert aggregate_counts(procurement_fixture) == before == (
        1, 1, 2, 1, 2, 0, 1, 1
    )


def test_past_exact_operation_with_different_payload_is_operation_conflict(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    clock = {"now": NOW}
    service = procurement_fixture.service(now_factory=lambda: clock["now"])
    with procurement_fixture.sessions() as db:
        submit(
            procurement_fixture,
            db,
            operation_id=operation_id,
            command=submission_command(),
            service=service,
        )
    before = aggregate_counts(procurement_fixture)
    clock["now"] = datetime(2030, 2, 2, tzinfo=timezone.utc)

    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="operation_id_conflict") as error:
            submit(
                procurement_fixture,
                db,
                operation_id=operation_id,
                command=submission_command(title="不同载荷"),
                service=service,
            )

    assert error.value.code == "operation_id_conflict"
    assert aggregate_counts(procurement_fixture) == before


def test_exact_replay_accepts_aggregate_after_real_approval_completion(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    command = submission_command()
    with procurement_fixture.sessions() as db:
        first = submit(
            procurement_fixture,
            db,
            operation_id=operation_id,
            command=command,
        )
        first_task_id, second_task_id = [task.id for task in first.tasks]

    with procurement_fixture.sessions() as db:
        engine = ApprovalEngine()
        actor = procurement_fixture.actor(db, procurement_fixture.manager_id)
        engine.approve_task(
            db,
            task_id=first_task_id,
            actor=actor,
            client_operation_id=uuid.uuid4(),
            comment="部门审批通过",
            authorize=AllowApprovalDecision(),
            now=NOW + timedelta(minutes=1),
        )
        completed = engine.approve_task(
            db,
            task_id=second_task_id,
            actor=actor,
            client_operation_id=uuid.uuid4(),
            comment="采购复核通过",
            authorize=AllowApprovalDecision(),
            now=NOW + timedelta(minutes=2),
        )
        db.commit()
        assert completed.instance.status is ApprovalInstanceStatus.APPROVED
        assert completed.instance.current_step_key is None
        assert completed.instance.completed_at is not None
        assert all(
            task.status is ApprovalTaskStatus.APPROVED
            and task.completed_at is not None
            for task in completed.tasks
        )
    before = aggregate_counts(procurement_fixture)

    with procurement_fixture.sessions() as db:
        replay = submit(
            procurement_fixture,
            db,
            operation_id=operation_id,
            command=command,
        )

    assert replay.replayed is True
    assert replay.request.id == first.request.id
    assert replay.instance.status is ApprovalInstanceStatus.APPROVED
    assert replay.instance.current_step_key is None
    assert replay.instance.completed_at is not None
    assert all(
        task.status is ApprovalTaskStatus.APPROVED
        and task.completed_at is not None
        for task in replay.tasks
    )
    assert aggregate_counts(procurement_fixture) == before == (
        1, 1, 2, 1, 2, 2, 1, 1
    )


def test_same_actor_operation_different_payload_is_stable_conflict_without_writes(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    with procurement_fixture.sessions() as db:
        submit(procurement_fixture, db, operation_id=operation_id)
        before = aggregate_counts(procurement_fixture)
        with pytest.raises(ToolError, match="operation_id_conflict") as error:
            submit(procurement_fixture, db, operation_id=operation_id, command=submission_command(title="changed"))
        assert error.value.code == "operation_id_conflict"
    assert aggregate_counts(procurement_fixture) == before


@pytest.mark.parametrize(
    ("command_kind", "status", "result_type"),
    [
        ("procurement.withdraw", ProcurementCommandOperationStatus.SUCCEEDED, "procurement_request"),
        ("procurement.submit", ProcurementCommandOperationStatus.IN_PROGRESS, None),
        ("procurement.submit", ProcurementCommandOperationStatus.SUCCEEDED, "approval_instance"),
    ],
)
def test_same_operation_invalid_command_or_result_shape_is_conflict(
    procurement_fixture: ProcurementFixture,
    command_kind: str,
    status: ProcurementCommandOperationStatus,
    result_type: str | None,
) -> None:
    operation_id = uuid.uuid4()
    command = submission_command()
    with procurement_fixture.sessions.begin() as db:
        db.add(
            ProcurementCommandOperation(
                id=uuid.uuid4(), actor_user_id=procurement_fixture.applicant_id,
                client_operation_id=operation_id, command_kind=command_kind,
                canonical_payload_hash=canonical_submission_hash(command), status=status,
                result_resource_type=result_type,
                result_resource_id=uuid.uuid4() if result_type is not None else None,
            )
        )
    before = aggregate_counts(procurement_fixture)
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="operation_id_conflict"):
            submit(procurement_fixture, db, operation_id=operation_id, command=command)
    assert aggregate_counts(procurement_fixture) == before


class SynchronizedClaimRepository(ProcurementRepository):
    def __init__(self, barrier: Barrier) -> None:
        self.barrier = barrier

    def claim_operation(self, db: Session, **values):
        self.barrier.wait(timeout=10)
        return super().claim_operation(db, **values)


def test_two_real_sessions_concurrent_exact_command_create_once_and_replay(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    barrier = Barrier(2)

    def run():
        with procurement_fixture.sessions() as db:
            result = submit(
                procurement_fixture,
                db,
                operation_id=operation_id,
                service=procurement_fixture.service(repository=SynchronizedClaimRepository(barrier)),
            )
            return result.request.id, result.replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run), executor.submit(run)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0][0] == results[1][0]
    assert sorted(replayed for _, replayed in results) == [False, True]
    assert aggregate_counts(procurement_fixture) == (1, 1, 2, 1, 2, 0, 1, 1)


def test_operation_id_is_actor_scoped_and_product_event_id_is_globally_safe(
    procurement_fixture: ProcurementFixture,
) -> None:
    second_actor = procurement_fixture.add_applicant()
    operation_id = uuid.uuid4()
    with procurement_fixture.sessions() as db:
        first = submit(procurement_fixture, db, operation_id=operation_id)
        second = submit(procurement_fixture, db, operation_id=operation_id, actor_id=second_actor)
        event_ids = tuple(db.scalars(select(ProductEvent.event_id).where(ProductEvent.actor_user_id.in_((procurement_fixture.applicant_id, second_actor)))))
        assert first.request.id != second.request.id
        assert len(event_ids) == len(set(event_ids)) == 2
    assert aggregate_counts(procurement_fixture) == (2, 2, 4, 2, 4, 0, 2, 2)


def test_request_number_unique_conflict_rolls_back_second_operation_and_aggregate(
    procurement_fixture: ProcurementFixture,
) -> None:
    fixed_number = "PR-FIXED-UNIQUE-CONFLICT"
    service = procurement_fixture.service(request_number_factory=lambda: fixed_number)
    with procurement_fixture.sessions() as db:
        submit(procurement_fixture, db, service=service)
        before = aggregate_counts(procurement_fixture)
        with pytest.raises(ToolError, match="procurement_request_conflict"):
            submit(procurement_fixture, db, service=service)
    assert aggregate_counts(procurement_fixture) == before == (1, 1, 2, 1, 2, 0, 1, 1)


def test_commit_false_leaves_transaction_to_outer_rollback(
    procurement_fixture: ProcurementFixture,
) -> None:
    with procurement_fixture.sessions() as db:
        submit(procurement_fixture, db, commit=False)
        assert db.in_transaction()
        db.rollback()
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


def test_commit_false_outer_commit_persists_complete_aggregate(
    procurement_fixture: ProcurementFixture,
) -> None:
    with procurement_fixture.sessions() as db:
        result = submit(procurement_fixture, db, commit=False)
        assert result.replayed is False
        db.commit()
    assert aggregate_counts(procurement_fixture) == (1, 1, 2, 1, 2, 0, 1, 1)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing_profile", "procurement_profile_required"),
        ("inactive_profile", "procurement_profile_required"),
        ("missing_organization", "procurement_profile_required"),
        ("inactive_organization", "procurement_profile_required"),
        ("missing_manager", "procurement_manager_unavailable"),
        ("inactive_manager_profile", "procurement_manager_unavailable"),
        ("inactive_manager_user", "procurement_manager_unavailable"),
        ("missing_manager_capability", "procurement_manager_capability_required"),
    ],
)
def test_authoritative_prerequisites_fail_closed_before_operation_claim(
    procurement_fixture: ProcurementFixture, mutation: str, expected_code: str
) -> None:
    with procurement_fixture.sessions.begin() as db:
        applicant = db.get(EmployeeProfile, procurement_fixture.applicant_employee_id)
        manager = db.get(EmployeeProfile, procurement_fixture.manager_employee_id)
        unit = db.get(OrganizationUnit, procurement_fixture.organization_id)
        manager_user = db.get(User, procurement_fixture.manager_id)
        assert applicant is not None and manager is not None and unit is not None and manager_user is not None
        if mutation == "missing_profile":
            db.delete(applicant)
        elif mutation == "inactive_profile":
            applicant.is_active = False
        elif mutation == "missing_organization":
            applicant.organization_unit_id = None
        elif mutation == "inactive_organization":
            unit.is_active = False
        elif mutation == "missing_manager":
            applicant.manager_employee_id = None
        elif mutation == "inactive_manager_profile":
            manager.is_active = False
        elif mutation == "inactive_manager_user":
            manager_user.is_active = False
        else:
            db.execute(delete(CapabilityGrant).where(CapabilityGrant.user_id == manager_user.id))
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match=expected_code) as error:
            submit(procurement_fixture, db)
        assert error.value.code == expected_code
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


def test_manager_role_never_bypasses_scoped_capability_resolver(
    procurement_fixture: ProcurementFixture,
) -> None:
    with procurement_fixture.sessions.begin() as db:
        manager = db.get(User, procurement_fixture.manager_id)
        assert manager is not None
        manager.role = UserRole.ADMIN
        db.execute(delete(CapabilityGrant).where(CapabilityGrant.user_id == manager.id))
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="procurement_manager_capability_required"):
            submit(procurement_fixture, db)
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


def test_new_operation_with_past_needed_date_rolls_back_claim_and_aggregate(
    procurement_fixture: ProcurementFixture,
) -> None:
    past = SubmitProcurementRequestCommand(
        title="过去日期",
        purpose="必须拒绝",
        needed_by_date=NOW.date().replace(day=NOW.day - 1),
        currency=ProcurementCurrencyCode.CNY,
        items=submission_command().items,
    )
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="procurement_needed_date_invalid"):
            submit(procurement_fixture, db, command=past)
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


def test_detached_actor_is_reloaded_and_inactive_database_user_is_rejected(
    procurement_fixture: ProcurementFixture,
) -> None:
    with procurement_fixture.sessions() as db:
        detached = fixture_actor = procurement_fixture.actor(db)
        db.expunge(fixture_actor)
    with procurement_fixture.sessions.begin() as db:
        authoritative = db.get(User, procurement_fixture.applicant_id)
        assert authoritative is not None
        authoritative.is_active = False
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="procurement_profile_required"):
            submit(procurement_fixture, db, actor=detached)
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


def test_admin_profile_requires_explicit_procurement_self_service_grant(
    procurement_fixture: ProcurementFixture,
) -> None:
    with procurement_fixture.sessions.begin() as db:
        actor = db.get(User, procurement_fixture.applicant_id)
        assert actor is not None
        actor.role = UserRole.ADMIN
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="procurement_profile_required"):
            submit(procurement_fixture, db)
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


def test_admin_profile_with_explicit_self_service_grant_can_submit(
    procurement_fixture: ProcurementFixture,
) -> None:
    with procurement_fixture.sessions.begin() as db:
        actor = db.get(User, procurement_fixture.applicant_id)
        assert actor is not None
        actor.role = UserRole.ADMIN
        db.add(
            CapabilityGrant(
                id=uuid.uuid4(),
                user_id=actor.id,
                capability=Capability.PROCUREMENT_REQUEST_SELF_SERVICE.value,
                scope_kind=ScopeKind.GLOBAL.value,
                organization_unit_id=None,
                is_active=True,
            )
        )
    with procurement_fixture.sessions() as db:
        result = submit(procurement_fixture, db)
        assert result.request.id is not None
    assert aggregate_counts(procurement_fixture) == (1, 1, 2, 1, 2, 0, 1, 1)


def test_admin_self_service_grant_for_other_scope_is_rejected(
    procurement_fixture: ProcurementFixture,
) -> None:
    with procurement_fixture.sessions.begin() as db:
        actor = db.get(User, procurement_fixture.applicant_id)
        assert actor is not None
        actor.role = UserRole.ADMIN
        other = OrganizationUnit(
            id=uuid.uuid4(), code=f"{procurement_fixture.prefix}-OTHER-SCOPE",
            name="Other Scope", is_active=True,
        )
        db.add(other)
        db.flush()
        db.add(
            CapabilityGrant(
                id=uuid.uuid4(), user_id=actor.id,
                capability=Capability.PROCUREMENT_REQUEST_SELF_SERVICE.value,
                scope_kind=ScopeKind.UNIT_SUBTREE.value,
                organization_unit_id=other.id, is_active=True,
            )
        )
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="procurement_profile_required"):
            submit(procurement_fixture, db)
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("role", [UserRole.EMPLOYEE, UserRole.HR])
def test_employee_and_hr_profiles_have_implicit_self_service(
    procurement_fixture: ProcurementFixture, role: UserRole
) -> None:
    with procurement_fixture.sessions.begin() as db:
        actor = db.get(User, procurement_fixture.applicant_id)
        assert actor is not None
        actor.role = role
    with procurement_fixture.sessions() as db:
        submit(procurement_fixture, db)
    assert aggregate_counts(procurement_fixture) == (1, 1, 2, 1, 2, 0, 1, 1)


def test_authoritative_actor_role_and_identity_drive_instance_and_event_snapshot(
    procurement_fixture: ProcurementFixture,
) -> None:
    with procurement_fixture.sessions() as db:
        detached = procurement_fixture.actor(db)
        db.expunge(detached)
    detached.role = UserRole.ADMIN
    with procurement_fixture.sessions() as db:
        result = submit(procurement_fixture, db, actor=detached)
        event = db.scalar(
            select(ProductEvent).where(
                ProductEvent.actor_user_id == procurement_fixture.applicant_id
            )
        )
        assert event is not None
        assert result.instance.applicant_user_id == procurement_fixture.applicant_id
        assert event.actor_user_id == procurement_fixture.applicant_id
        assert event.role_snapshot == UserRole.EMPLOYEE.value


def _corrupt_submission_aggregate(
    db: Session,
    fixture: ProcurementFixture,
    request_id: uuid.UUID,
    mutation: str,
) -> None:
    request = db.get(ProcurementRequest, request_id)
    assert request is not None
    instance = db.get(ApprovalInstance, request.approval_instance_id)
    assert instance is not None
    items = list(
        db.scalars(
            select(ProcurementRequestItem)
            .where(ProcurementRequestItem.request_id == request.id)
            .order_by(ProcurementRequestItem.line_number)
        )
    )
    tasks = list(
        db.scalars(
            select(ApprovalTask)
            .where(ApprovalTask.instance_id == instance.id)
            .order_by(ApprovalTask.sequence)
        )
    )
    if mutation == "zero_items":
        for item in items:
            db.delete(item)
    elif mutation == "too_many_items":
        template = items[0]
        for line in range(3, 52):
            db.add(
                ProcurementRequestItem(
                    id=uuid.uuid4(), request_id=request.id, line_number=line,
                    category_code=template.category_code,
                    item_name=template.item_name, specification=template.specification,
                    quantity=template.quantity, unit=template.unit,
                    estimated_unit_price=template.estimated_unit_price,
                    subtotal=template.subtotal,
                )
            )
    elif mutation == "line_gap":
        items[1].line_number = 3
    elif mutation == "zero_tasks":
        for task in tasks:
            db.delete(task)
    elif mutation == "one_task":
        db.delete(tasks[1])
    elif mutation == "three_tasks":
        db.add(
            ApprovalTask(
                id=uuid.uuid4(), instance_id=instance.id, sequence=3,
                step_key="unexpected", step_label="Unexpected",
                assignment_kind=AssignmentKind.CAPABILITY,
                assigned_user_id=None,
                required_capability=Capability.PROCUREMENT_FINAL_REVIEW.value,
                scope_organization_unit_id=fixture.organization_id,
                status=ApprovalTaskStatus.WAITING,
                activated_at=None, completed_at=None,
            )
        )
    elif mutation == "process_key":
        instance.process_key = "procurement.other"
    elif mutation == "process_version":
        instance.process_version = 2
    elif mutation == "subject_type":
        instance.subject_type = "other_subject"
    elif mutation == "step_key":
        tasks[0].step_key = "wrong_step"
    elif mutation == "sequence":
        tasks[0].sequence = 3
    elif mutation == "first_assignment":
        tasks[0].assignment_kind = AssignmentKind.CAPABILITY
        tasks[0].assigned_user_id = None
        tasks[0].required_capability = Capability.PROCUREMENT_DEPARTMENT_REVIEW.value
        tasks[0].scope_organization_unit_id = fixture.organization_id
    elif mutation == "final_capability":
        tasks[1].required_capability = Capability.PROCUREMENT_DEPARTMENT_REVIEW.value
    elif mutation == "final_scope":
        tasks[1].scope_organization_unit_id = None
    elif mutation == "request_applicant":
        request.applicant_employee_id = fixture.manager_employee_id
    elif mutation == "request_organization":
        other = OrganizationUnit(
            id=uuid.uuid4(), code=f"{fixture.prefix}-OTHER",
            name="Other", is_active=True,
        )
        db.add(other)
        db.flush()
        request.organization_unit_id = other.id
    elif mutation == "request_number_empty":
        request.request_number = ""
    elif mutation == "request_fields":
        request.title = "changed"
    elif mutation == "item_fields":
        items[0].item_name = "changed"
    elif mutation == "subtotal":
        items[0].subtotal += Decimal("1.00")
    elif mutation == "total":
        request.total_amount += Decimal("1.00")
    elif mutation == "missing_audit":
        audit = db.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.actor_user_id == fixture.applicant_id
            )
        )
        assert audit is not None
        db.delete(audit)
    elif mutation == "missing_event":
        event = db.scalar(
            select(ProductEvent).where(
                ProductEvent.actor_user_id == fixture.applicant_id
            )
        )
        assert event is not None
        db.delete(event)
    elif mutation == "audit_target":
        audit = db.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.actor_user_id == fixture.applicant_id
            )
        )
        assert audit is not None
        audit.target_id = uuid.uuid4()
    elif mutation == "event_actor":
        event = db.scalar(
            select(ProductEvent).where(
                ProductEvent.actor_user_id == fixture.applicant_id
            )
        )
        assert event is not None
        event.actor_user_id = fixture.manager_id
    elif mutation == "event_role":
        event = db.scalar(
            select(ProductEvent).where(
                ProductEvent.actor_user_id == fixture.applicant_id
            )
        )
        assert event is not None
        event.role_snapshot = "system"
    elif mutation == "first_step_label":
        tasks[0].step_label = "Wrong department label"
    elif mutation == "final_step_label":
        tasks[1].step_label = "Wrong procurement label"
    elif mutation == "event_duration":
        event = db.scalar(
            select(ProductEvent).where(
                ProductEvent.actor_user_id == fixture.applicant_id
            )
        )
        assert event is not None
        event.duration_ms = 1
    else:
        raise AssertionError(mutation)


@pytest.mark.parametrize(
    "mutation",
    [
        "zero_items", "too_many_items", "line_gap",
        "zero_tasks", "one_task", "three_tasks",
        "process_key", "process_version", "subject_type",
        "step_key", "sequence", "first_assignment",
        "final_capability", "final_scope",
        "request_applicant", "request_organization", "request_number_empty",
        "request_fields", "first_step_label", "final_step_label",
        "item_fields", "subtotal", "total", "missing_audit", "missing_event",
        "audit_target", "event_actor", "event_role", "event_duration",
    ],
)
def test_replay_rejects_malformed_or_incomplete_authoritative_aggregate(
    procurement_fixture: ProcurementFixture, mutation: str
) -> None:
    operation_id = uuid.uuid4()
    command = submission_command()
    with procurement_fixture.sessions() as db:
        first = submit(
            procurement_fixture, db, operation_id=operation_id, command=command
        )
        resource_id = first.request.id
    with procurement_fixture.sessions.begin() as db:
        _corrupt_submission_aggregate(db, procurement_fixture, resource_id, mutation)
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="operation_id_conflict"):
            submit(
                procurement_fixture, db,
                operation_id=operation_id, command=command,
            )


class OverlongRequestNumberRepository(ProcurementRepository):
    def get_request(self, db: Session, request_id: uuid.UUID):
        request = super().get_request(db, request_id)
        assert request is not None
        set_committed_value(request, "request_number", "R" * 41)
        return request


def test_replay_rejects_overlong_authoritative_request_number(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    command = submission_command()
    with procurement_fixture.sessions() as db:
        submit(
            procurement_fixture, db, operation_id=operation_id, command=command
        )
    before = aggregate_counts(procurement_fixture)
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="operation_id_conflict"):
            submit(
                procurement_fixture,
                db,
                operation_id=operation_id,
                command=command,
                service=procurement_fixture.service(
                    repository=OverlongRequestNumberRepository()
                ),
            )
    assert aggregate_counts(procurement_fixture) == before


class WrongRequestItemRepository(ProcurementRepository):
    def list_items(self, db: Session, request_id: uuid.UUID):
        items = tuple(super().list_items(db, request_id))
        assert items
        items[0].request_id = uuid.uuid4()
        return items


def test_replay_rejects_item_returned_for_wrong_request_identity(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    with procurement_fixture.sessions() as db:
        submit(procurement_fixture, db, operation_id=operation_id)
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="operation_id_conflict"):
            submit(
                procurement_fixture, db, operation_id=operation_id,
                service=procurement_fixture.service(
                    repository=WrongRequestItemRepository()
                ),
            )


def test_concurrent_same_operation_different_payload_has_one_winner_and_conflict(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    barrier = Barrier(2)

    def run(command: SubmitProcurementRequestCommand):
        with procurement_fixture.sessions() as db:
            try:
                result = submit(
                    procurement_fixture, db, operation_id=operation_id,
                    command=command,
                    service=procurement_fixture.service(
                        repository=SynchronizedClaimRepository(barrier)
                    ),
                )
                return "ok", result.request.id
            except ToolError as error:
                return "error", error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(run, submission_command(title="payload-a")),
            executor.submit(run, submission_command(title="payload-b")),
        ]
        results = [future.result(timeout=20) for future in futures]
    assert sum(kind == "ok" for kind, _ in results) == 1
    assert ("error", "operation_id_conflict") in results
    assert aggregate_counts(procurement_fixture) == (1, 1, 2, 1, 2, 0, 1, 1)


class RollbackAfterClaimRepository(ProcurementRepository):
    def __init__(self, claimed: Event) -> None:
        self.claimed = claimed

    def claim_operation(self, db: Session, **values):
        operation, created = super().claim_operation(db, **values)
        assert created is True
        self.claimed.set()
        raise RuntimeError("winner rolls back")


class WaitForRollbackRepository(ProcurementRepository):
    def __init__(self, claimed: Event) -> None:
        self.claimed = claimed

    def claim_operation(self, db: Session, **values):
        assert self.claimed.wait(timeout=10)
        return super().claim_operation(db, **values)


def test_waiter_takes_over_claim_after_concurrent_winner_rolls_back(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    claimed = Event()

    def failing():
        with procurement_fixture.sessions() as db:
            with pytest.raises(RuntimeError, match="winner rolls back"):
                submit(
                    procurement_fixture, db, operation_id=operation_id,
                    service=procurement_fixture.service(
                        repository=RollbackAfterClaimRepository(claimed)
                    ),
                )
        return "rolled_back"

    def waiting():
        with procurement_fixture.sessions() as db:
            result = submit(
                procurement_fixture, db, operation_id=operation_id,
                service=procurement_fixture.service(
                    repository=WaitForRollbackRepository(claimed)
                ),
            )
            return result.replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        failed = executor.submit(failing)
        waiter = executor.submit(waiting)
        assert failed.result(timeout=20) == "rolled_back"
        assert waiter.result(timeout=20) is False
    assert aggregate_counts(procurement_fixture) == (1, 1, 2, 1, 2, 0, 1, 1)


class TransactionSpySession(Session):
    commit_calls = 0
    rollback_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1
        super().commit()

    def rollback(self) -> None:
        self.rollback_calls += 1
        super().rollback()


def _spy_session(fixture: ProcurementFixture) -> TransactionSpySession:
    return TransactionSpySession(
        bind=fixture.sessions.kw["bind"], expire_on_commit=False, autoflush=False
    )


def test_commit_false_dependency_failure_never_commits_or_rolls_back_service(
    procurement_fixture: ProcurementFixture,
) -> None:
    db = _spy_session(procurement_fixture)
    try:
        with pytest.raises(RuntimeError, match="product event failed"):
            submit(
                procurement_fixture, db, commit=False,
                service=procurement_fixture.service(
                    product_event_emitter=FailingEventEmitter()
                ),
            )
        assert (db.commit_calls, db.rollback_calls) == (0, 0)
        assert db.in_transaction()
        db.rollback()
    finally:
        db.close()
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


def test_commit_false_integrity_error_leaves_failed_transaction_to_outer_owner(
    procurement_fixture: ProcurementFixture,
) -> None:
    db = _spy_session(procurement_fixture)
    try:
        with pytest.raises(ToolError, match="procurement_request_conflict"):
            submit(
                procurement_fixture, db, commit=False,
                service=procurement_fixture.service(
                    repository=CorruptAtCompletionRepository("request_not_null")
                ),
            )
        assert (db.commit_calls, db.rollback_calls) == (0, 0)
        with pytest.raises(Exception):
            db.execute(select(User.id))
        db.rollback()
    finally:
        db.close()
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)
