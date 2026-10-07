from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import uuid

import pytest
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from policy_api.approvals.authorization import ApprovalAuthorizationPort
from policy_api.approvals.enums import (
    ApprovalCommandKind,
    ApprovalCommandStatus,
    ApprovalInstanceStatus,
    ApprovalTaskStatus,
    AssignmentKind,
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
from policy_api.models import User, UserRole
from policy_api.tools.errors import ToolError
from policy_api.workbench.capabilities import OrganizationUnit

TEST_DATABASE_URL = (
    "postgresql+psycopg://policy_test@127.0.0.1:55434/"
    "procurement_approval_task2_test"
)
NOW = datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)


class AllowAll(ApprovalAuthorizationPort):
    def authorize(self, *, instance, task, actor, action) -> None:
        return None


class SynchronizedLockRepository(ApprovalRepository):
    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier

    def lock_instance(self, db: Session, instance_id: uuid.UUID):
        self._barrier.wait(timeout=5)
        return super().lock_instance(db, instance_id)


@dataclass(frozen=True)
class ApprovalRows:
    applicant_id: uuid.UUID
    manager_id: uuid.UUID
    instance_id: uuid.UUID
    first_task_id: uuid.UUID
    second_task_id: uuid.UUID
    unit_id: uuid.UUID


@pytest.fixture()
def database():
    assert_test_database_url(TEST_DATABASE_URL)
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as connection:
        if not connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'approval_command_operations')"
            )
        ).scalar_one():
            pytest.fail("approval migration head is required")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def approval_rows(database):
    factory = sessionmaker(bind=database, expire_on_commit=False, autoflush=False)
    applicant = User(
        id=uuid.uuid4(),
        username=f"approval-applicant-{uuid.uuid4()}",
        password_hash="hash",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )
    manager = User(
        id=uuid.uuid4(),
        username=f"approval-manager-{uuid.uuid4()}",
        password_hash="hash",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )
    unit = OrganizationUnit(
        id=uuid.uuid4(), code=f"APR-{uuid.uuid4()}", name="Approval Test", is_active=True
    )
    instance = ApprovalInstance(
        id=uuid.uuid4(),
        process_key="procurement.request",
        process_version=1,
        subject_type="procurement_request",
        applicant_user_id=applicant.id,
        organization_unit_id=unit.id,
        status=ApprovalInstanceStatus.RUNNING,
        current_step_key="department_manager_review",
        version=1,
        submitted_at=NOW,
    )
    first = ApprovalTask(
        id=uuid.uuid4(), instance_id=instance.id, sequence=1,
        step_key="department_manager_review", step_label="Department review",
        assignment_kind=AssignmentKind.USER, assigned_user_id=manager.id,
        status=ApprovalTaskStatus.PENDING, activated_at=NOW,
    )
    second = ApprovalTask(
        id=uuid.uuid4(), instance_id=instance.id, sequence=2,
        step_key="procurement_review", step_label="Procurement review",
        assignment_kind=AssignmentKind.CAPABILITY,
        required_capability="procurement.final.review",
        scope_organization_unit_id=unit.id,
        status=ApprovalTaskStatus.WAITING,
    )
    with factory() as db:
        db.add_all([applicant, manager, unit])
        db.flush()
        db.add(instance)
        db.flush()
        db.add_all([first, second])
        db.commit()
    rows = ApprovalRows(
        applicant.id, manager.id, instance.id, first.id, second.id, unit.id
    )
    try:
        yield rows
    finally:
        with factory.begin() as db:
            db.execute(
                delete(ApprovalDecision).where(ApprovalDecision.instance_id == instance.id)
            )
            db.execute(
                delete(ApprovalCommandOperation).where(
                    ApprovalCommandOperation.instance_id == instance.id
                )
            )
            db.execute(delete(ApprovalTask).where(ApprovalTask.instance_id == instance.id))
            db.execute(
                delete(ApprovalInstance).where(ApprovalInstance.id == instance.id)
            )
            db.execute(delete(OrganizationUnit).where(OrganizationUnit.id == unit.id))
            db.execute(delete(User).where(User.id.in_([applicant.id, manager.id])))


def _actor(db: Session, actor_id: uuid.UUID) -> User:
    actor = db.get(User, actor_id)
    assert actor is not None
    return actor


def _run_approve(
    database,
    rows: ApprovalRows,
    operation_id: uuid.UUID,
    repository: ApprovalRepository,
) -> tuple[str, object]:
    factory = sessionmaker(bind=database, expire_on_commit=False, autoflush=False)
    with factory() as db:
        db.execute(text("SET LOCAL statement_timeout = '5s'"))
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        try:
            result = ApprovalEngine(repository).approve_task(
                db,
                task_id=rows.first_task_id,
                actor=_actor(db, rows.manager_id),
                client_operation_id=operation_id,
                comment=None,
                authorize=AllowAll(),
                now=NOW,
            )
            db.commit()
            return "ok", result.replayed
        except ToolError as error:
            db.rollback()
            return "error", error.code


def _run_cancel(
    database,
    rows: ApprovalRows,
    operation_id: uuid.UUID,
    repository: ApprovalRepository,
) -> tuple[str, object]:
    factory = sessionmaker(bind=database, expire_on_commit=False, autoflush=False)
    with factory() as db:
        db.execute(text("SET LOCAL statement_timeout = '5s'"))
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        try:
            result = ApprovalEngine(repository).cancel_instance(
                db,
                instance_id=rows.instance_id,
                actor_user_id=rows.applicant_id,
                client_operation_id=operation_id,
                now=NOW,
            )
            db.commit()
            return "ok", result.replayed
        except ToolError as error:
            db.rollback()
            return "error", error.code


def _two_results(first, second) -> list[tuple[str, object]]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(first), executor.submit(second)]
        return [future.result(timeout=10) for future in futures]


def test_concurrent_approve_approve_has_one_winner_and_one_stable_conflict(
    database, approval_rows: ApprovalRows
) -> None:
    barrier = threading.Barrier(2)
    results = _two_results(
        lambda: _run_approve(
            database, approval_rows, uuid.uuid4(), SynchronizedLockRepository(barrier)
        ),
        lambda: _run_approve(
            database, approval_rows, uuid.uuid4(), SynchronizedLockRepository(barrier)
        ),
    )

    assert sorted(results) == [
        ("error", "approval_task_state_conflict"),
        ("ok", False),
    ]
    with Session(database) as db:
        assert db.scalar(
            select(func.count()).select_from(ApprovalDecision).where(
                ApprovalDecision.instance_id == approval_rows.instance_id
            )
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(ApprovalCommandOperation).where(
                ApprovalCommandOperation.instance_id == approval_rows.instance_id
            )
        ) == 1


def test_concurrent_approve_cancel_has_exactly_one_winner(
    database, approval_rows: ApprovalRows
) -> None:
    barrier = threading.Barrier(2)
    results = _two_results(
        lambda: _run_approve(
            database, approval_rows, uuid.uuid4(), SynchronizedLockRepository(barrier)
        ),
        lambda: _run_cancel(
            database, approval_rows, uuid.uuid4(), SynchronizedLockRepository(barrier)
        ),
    )

    assert sum(result[0] == "ok" for result in results) == 1
    assert sum(result[0] == "error" for result in results) == 1
    assert next(result[1] for result in results if result[0] == "error") in {
        "approval_instance_state_conflict",
        "approval_task_state_conflict",
    }
    with Session(database) as db:
        instance = db.get(ApprovalInstance, approval_rows.instance_id)
        assert instance is not None
        assert instance.status in {
            ApprovalInstanceStatus.RUNNING,
            ApprovalInstanceStatus.CANCELLED,
        }
        assert db.scalar(
            select(func.count()).select_from(ApprovalCommandOperation).where(
                ApprovalCommandOperation.instance_id == approval_rows.instance_id
            )
        ) == 1


def test_concurrent_same_operation_cancel_claims_once_and_replays(
    database, approval_rows: ApprovalRows
) -> None:
    operation_id = uuid.uuid4()
    barrier = threading.Barrier(2)
    results = _two_results(
        lambda: _run_cancel(
            database, approval_rows, operation_id, SynchronizedLockRepository(barrier)
        ),
        lambda: _run_cancel(
            database, approval_rows, operation_id, SynchronizedLockRepository(barrier)
        ),
    )

    assert sorted(results) == [("ok", False), ("ok", True)]
    with Session(database) as db:
        assert db.scalar(
            select(func.count()).select_from(ApprovalCommandOperation).where(
                ApprovalCommandOperation.instance_id == approval_rows.instance_id
            )
        ) == 1

    result = _run_cancel(
        database, approval_rows, uuid.uuid4(), ApprovalRepository()
    )
    assert result == ("error", "approval_instance_state_conflict")


def test_locked_queries_refresh_stale_instance_and_task_before_new_decision(
    database, approval_rows: ApprovalRows
) -> None:
    factory = sessionmaker(bind=database, expire_on_commit=False, autoflush=False)
    with factory() as stale_db:
        cached_instance = stale_db.get(ApprovalInstance, approval_rows.instance_id)
        cached_task = stale_db.get(ApprovalTask, approval_rows.first_task_id)
        assert cached_instance is not None and cached_task is not None
        assert cached_instance.status is ApprovalInstanceStatus.RUNNING
        assert cached_task.status is ApprovalTaskStatus.PENDING

        assert _run_approve(
            database, approval_rows, uuid.uuid4(), ApprovalRepository()
        ) == ("ok", False)

        with pytest.raises(ToolError) as error:
            ApprovalEngine(ApprovalRepository()).approve_task(
                stale_db,
                task_id=approval_rows.first_task_id,
                actor=_actor(stale_db, approval_rows.manager_id),
                client_operation_id=uuid.uuid4(),
                comment=None,
                authorize=AllowAll(),
                now=NOW,
            )
        stale_db.rollback()

    assert error.value.code == "approval_task_state_conflict"


def test_exact_replay_refreshes_stale_operation_and_returns_current_tasks(
    database, approval_rows: ApprovalRows
) -> None:
    operation_id = uuid.uuid4()
    payload_hash = ApprovalEngine._payload_hash(
        command_kind=ApprovalCommandKind.CANCEL,
        instance_id=approval_rows.instance_id,
        task_id=None,
        comment=None,
    )
    factory = sessionmaker(bind=database, expire_on_commit=False, autoflush=False)
    with factory.begin() as setup_db:
        setup_db.add(
            ApprovalCommandOperation(
                id=uuid.uuid4(),
                actor_user_id=approval_rows.applicant_id,
                client_operation_id=operation_id,
                command_kind=ApprovalCommandKind.CANCEL,
                instance_id=approval_rows.instance_id,
                task_id=None,
                canonical_payload_hash=payload_hash,
                status=ApprovalCommandStatus.IN_PROGRESS,
                completed_at=None,
            )
        )

    with factory() as stale_db:
        cached_instance = stale_db.get(ApprovalInstance, approval_rows.instance_id)
        cached_tasks = tuple(
            stale_db.scalars(
                select(ApprovalTask)
                .where(ApprovalTask.instance_id == approval_rows.instance_id)
                .order_by(ApprovalTask.sequence)
            )
        )
        cached_operation = stale_db.scalar(
            select(ApprovalCommandOperation).where(
                ApprovalCommandOperation.actor_user_id == approval_rows.applicant_id,
                ApprovalCommandOperation.client_operation_id == operation_id,
            )
        )
        assert cached_instance is not None and cached_operation is not None
        assert cached_operation.status is ApprovalCommandStatus.IN_PROGRESS
        assert [task.status for task in cached_tasks] == [
            ApprovalTaskStatus.PENDING,
            ApprovalTaskStatus.WAITING,
        ]

        with factory.begin() as advancing_db:
            authoritative_instance = advancing_db.get(
                ApprovalInstance, approval_rows.instance_id
            )
            authoritative_operation = advancing_db.get(
                ApprovalCommandOperation, cached_operation.id
            )
            authoritative_tasks = tuple(
                advancing_db.scalars(
                    select(ApprovalTask).where(
                        ApprovalTask.instance_id == approval_rows.instance_id
                    )
                )
            )
            assert authoritative_instance is not None
            assert authoritative_operation is not None
            authoritative_instance.status = ApprovalInstanceStatus.CANCELLED
            authoritative_instance.current_step_key = None
            authoritative_instance.completed_at = NOW
            authoritative_instance.version += 1
            for task in authoritative_tasks:
                task.status = ApprovalTaskStatus.CANCELLED
                task.completed_at = NOW
            authoritative_operation.status = ApprovalCommandStatus.SUCCEEDED
            authoritative_operation.completed_at = NOW

        result = ApprovalEngine(ApprovalRepository()).cancel_instance(
            stale_db,
            instance_id=approval_rows.instance_id,
            actor_user_id=approval_rows.applicant_id,
            client_operation_id=operation_id,
            now=NOW,
        )
        assert result.replayed is True
        assert result.operation.status is ApprovalCommandStatus.SUCCEEDED
        assert result.instance.status is ApprovalInstanceStatus.CANCELLED
        returned_task_statuses = [task.status for task in result.tasks]
        stale_db.rollback()

    assert returned_task_statuses == [
        ApprovalTaskStatus.CANCELLED,
        ApprovalTaskStatus.CANCELLED,
    ]
