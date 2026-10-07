from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
from threading import Barrier
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from policy_api.database import assert_test_database_url
from policy_api.hr.enums import (
    LeaveAccountEventKind,
    LeaveRequestStatus,
    LeaveTypeCode,
    WorkCalendarDayKind,
)
from policy_api.hr.models import (
    EmployeeProfile,
    LeaveAccount,
    LeaveAccountEvent,
    LeaveRequest,
    LeaveType,
    WorkCalendarDay,
)
from policy_api.hr.schemas import HrDomainError
from policy_api.hr.service import (
    approve_leave_request,
    cancel_leave_request,
    reject_leave_request,
    submit_leave_request,
)
from policy_api.models import User, UserRole
from policy_api.tools.enums import ToolConfirmationStatus, ToolInvocationStatus
from policy_api.tools.models import ToolAuditEvent, ToolConfirmation, ToolInvocation
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityResolver,
    CapabilityScope,
    OrganizationUnit,
    ScopeKind,
)


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


@dataclass(frozen=True, slots=True)
class LeaveFixture:
    sessions: sessionmaker[Session]
    employee_user_id: uuid.UUID
    reviewer_user_id: uuid.UUID
    employee_id: uuid.UUID
    account_id: uuid.UUID
    leave_type_id: uuid.UUID
    start_date: date


@pytest.fixture
def leave_fixture() -> LeaveFixture:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for leave transaction tests")
    assert_test_database_url(TEST_DATABASE_URL)
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    original_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    command.upgrade(config, "head")
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    start = date(2031, 1, 6)
    with sessions() as db:
        employee_user = User(
            username=f"tx-employee-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        reviewer = User(
            username=f"tx-reviewer-{suffix}",
            password_hash="hash",
            role=UserRole.HR,
            is_active=True,
        )
        db.add_all([employee_user, reviewer])
        db.flush()
        employee = EmployeeProfile(
            user_id=employee_user.id,
            employee_number=f"TX-{suffix}",
            display_name="Transaction Employee",
            hire_date=date(2024, 1, 1),
            is_active=True,
        )
        leave_type = LeaveType(
            code=LeaveTypeCode.ANNUAL,
            display_name="Annual leave",
            is_enabled=True,
        )
        db.add_all([employee, leave_type])
        db.flush()
        account = LeaveAccount(
            employee_id=employee.id,
            leave_type_id=leave_type.id,
            year=start.year,
            entitled=Decimal("10.00"),
            used=Decimal("0.00"),
            reserved=Decimal("0.00"),
            version=1,
        )
        db.add(account)
        db.add_all(
            WorkCalendarDay(
                calendar_date=start + timedelta(days=offset),
                kind=WorkCalendarDayKind.WORKDAY,
                is_workday=True,
                label=None,
            )
            for offset in range(5)
        )
        db.commit()
        fixture = LeaveFixture(
            sessions=sessions,
            employee_user_id=employee_user.id,
            reviewer_user_id=reviewer.id,
            employee_id=employee.id,
            account_id=account.id,
            leave_type_id=leave_type.id,
            start_date=start,
        )
    try:
        yield fixture
    finally:
        with sessions() as db:
            actor_ids = [fixture.employee_user_id, fixture.reviewer_user_id]
            db.execute(delete(ToolAuditEvent).where(ToolAuditEvent.actor_user_id.in_(actor_ids)))
            db.execute(delete(ToolConfirmation).where(ToolConfirmation.owner_user_id.in_(actor_ids)))
            db.execute(delete(ToolInvocation).where(ToolInvocation.actor_user_id.in_(actor_ids)))
            db.execute(delete(LeaveAccountEvent).where(LeaveAccountEvent.actor_user_id.in_(actor_ids)))
            db.execute(
                delete(CapabilityGrant).where(
                    CapabilityGrant.user_id.in_(actor_ids)
                )
            )
            db.execute(delete(LeaveRequest).where(LeaveRequest.employee_id == fixture.employee_id))
            db.execute(delete(LeaveAccount).where(LeaveAccount.id == fixture.account_id))
            db.execute(
                delete(WorkCalendarDay).where(
                    WorkCalendarDay.calendar_date >= fixture.start_date,
                    WorkCalendarDay.calendar_date <= fixture.start_date + timedelta(days=4),
                )
            )
            db.execute(delete(LeaveType).where(LeaveType.id == fixture.leave_type_id))
            db.execute(delete(EmployeeProfile).where(EmployeeProfile.id == fixture.employee_id))
            db.execute(
                delete(OrganizationUnit).where(
                    OrganizationUnit.code.like("TX-SCOPE-%")
                )
            )
            db.execute(delete(User).where(User.id.in_(actor_ids)))
            db.commit()
        engine.dispose()
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url


def create_confirmation(
    db: Session,
    *,
    owner_user_id: uuid.UUID,
    tool_name: str,
    arguments: dict[str, object],
) -> ToolConfirmation:
    invocation = ToolInvocation(
        conversation_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        actor_user_id=owner_user_id,
        provider_call_id=f"call-{uuid.uuid4()}",
        tool_name=tool_name,
        provider_tool_name=tool_name.replace(".", "_"),
        risk_level="write",
        status=ToolInvocationStatus.CONFIRMATION_PENDING,
        arguments_hash="a" * 64,
    )
    db.add(invocation)
    db.flush()
    confirmation = ToolConfirmation(
        invocation_id=invocation.id,
        owner_user_id=owner_user_id,
        tool_name=tool_name,
        normalized_arguments=arguments,
        arguments_hash="a" * 64,
        preview=arguments,
        status=ToolConfirmationStatus.PENDING,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(confirmation)
    db.commit()
    return confirmation


def submit_confirmation(
    db: Session,
    fixture: LeaveFixture,
    *,
    start_offset: int = 0,
    days: int = 2,
    reason: str = "transaction test",
) -> ToolConfirmation:
    start = fixture.start_date + timedelta(days=start_offset)
    end = start + timedelta(days=days - 1)
    return create_confirmation(
        db,
        owner_user_id=fixture.employee_user_id,
        tool_name="hr.submit_leave_request",
        arguments={
            "leave_type_code": "annual",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "reason": reason,
        },
    )


def cancel_confirmation(
    db: Session,
    fixture: LeaveFixture,
    request_id: uuid.UUID,
) -> ToolConfirmation:
    return create_confirmation(
        db,
        owner_user_id=fixture.employee_user_id,
        tool_name="hr.cancel_leave_request",
        arguments={"request_id": str(request_id)},
    )


def account_state(db: Session, fixture: LeaveFixture) -> tuple[Decimal, Decimal]:
    account = db.get(LeaveAccount, fixture.account_id)
    assert account is not None
    return account.used, account.reserved


def grant_global_review(db: Session, fixture: LeaveFixture) -> None:
    db.add(
        CapabilityGrant(
            user_id=fixture.reviewer_user_id,
            capability=Capability.HR_LEAVE_REVIEW.value,
            scope_kind=ScopeKind.GLOBAL.value,
            is_active=True,
        )
    )
    db.commit()


def review_access(
    db: Session,
    fixture: LeaveFixture,
) -> tuple[CapabilityScope, CapabilityResolver]:
    reviewer = db.get(User, fixture.reviewer_user_id)
    assert reviewer is not None
    resolver = CapabilityResolver()
    scope = resolver.scope_for(db, reviewer, Capability.HR_LEAVE_REVIEW)
    assert scope is not None
    return scope, resolver


def test_submit_reserves_once_and_replays_same_operation(leave_fixture: LeaveFixture) -> None:
    with leave_fixture.sessions() as db:
        confirmation = submit_confirmation(db, leave_fixture)
        operation_id = uuid.uuid4()

        first = submit_leave_request(
            db, leave_fixture.employee_user_id, confirmation.id, operation_id
        )
        replay = submit_leave_request(
            db, leave_fixture.employee_user_id, confirmation.id, operation_id
        )

        assert first.id == replay.id
        assert first.status == LeaveRequestStatus.PENDING
        assert account_state(db, leave_fixture) == (Decimal("0.00"), Decimal("2.00"))
        assert db.scalar(
            select(func.count()).select_from(LeaveRequest).where(
                LeaveRequest.employee_id == leave_fixture.employee_id
            )
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(LeaveAccountEvent).where(
                LeaveAccountEvent.operation_id == operation_id
            )
        ) == 1


def test_submit_rejects_insufficient_balance_overlap_and_operation_reuse(
    leave_fixture: LeaveFixture,
) -> None:
    with leave_fixture.sessions() as db:
        account = db.get(LeaveAccount, leave_fixture.account_id)
        assert account is not None
        account.entitled = Decimal("1.00")
        db.commit()
        insufficient = submit_confirmation(db, leave_fixture, days=2)
        with pytest.raises(HrDomainError, match="leave_balance_insufficient"):
            submit_leave_request(
                db, leave_fixture.employee_user_id, insufficient.id, uuid.uuid4()
            )
        assert account_state(db, leave_fixture) == (Decimal("0.00"), Decimal("0.00"))

        account.entitled = Decimal("10.00")
        db.commit()
        operation_id = uuid.uuid4()
        first_confirmation = submit_confirmation(db, leave_fixture, days=2)
        submit_leave_request(
            db, leave_fixture.employee_user_id, first_confirmation.id, operation_id
        )
        overlap = submit_confirmation(db, leave_fixture, start_offset=1, days=2)
        with pytest.raises(HrDomainError, match="leave_request_overlap"):
            submit_leave_request(
                db, leave_fixture.employee_user_id, overlap.id, uuid.uuid4()
            )
        reused_operation = submit_confirmation(db, leave_fixture, start_offset=3, days=1)
        with pytest.raises(HrDomainError, match="operation_id_conflict"):
            submit_leave_request(
                db,
                leave_fixture.employee_user_id,
                reused_operation.id,
                operation_id,
            )


def test_approve_reject_and_cancel_move_reserved_atomically(
    leave_fixture: LeaveFixture,
) -> None:
    with leave_fixture.sessions() as db:
        grant_global_review(db, leave_fixture)
        review_scope, capability_resolver = review_access(db, leave_fixture)
        approved_confirmation = submit_confirmation(db, leave_fixture, days=1)
        approved = submit_leave_request(
            db, leave_fixture.employee_user_id, approved_confirmation.id, uuid.uuid4()
        )
        approve_operation = uuid.uuid4()
        approved_view = approve_leave_request(
            db,
            leave_fixture.reviewer_user_id,
            approved.id,
            approve_operation,
            review_scope=review_scope,
            capability_resolver=capability_resolver,
        )
        assert approved_view.status == LeaveRequestStatus.APPROVED
        assert approve_leave_request(
            db,
            leave_fixture.reviewer_user_id,
            approved.id,
            approve_operation,
            review_scope=review_scope,
            capability_resolver=capability_resolver,
        ).id == approved.id
        assert account_state(db, leave_fixture) == (Decimal("1.00"), Decimal("0.00"))
        with pytest.raises(HrDomainError, match="leave_request_state_conflict"):
            approve_leave_request(
                db,
                leave_fixture.reviewer_user_id,
                approved.id,
                uuid.uuid4(),
                review_scope=review_scope,
                capability_resolver=capability_resolver,
            )

        rejected_confirmation = submit_confirmation(db, leave_fixture, start_offset=1, days=1)
        rejected = submit_leave_request(
            db, leave_fixture.employee_user_id, rejected_confirmation.id, uuid.uuid4()
        )
        rejected_view = reject_leave_request(
            db,
            leave_fixture.reviewer_user_id,
            rejected.id,
            uuid.uuid4(),
            "staffing conflict",
            review_scope=review_scope,
            capability_resolver=capability_resolver,
        )
        assert rejected_view.status == LeaveRequestStatus.REJECTED
        assert account_state(db, leave_fixture) == (Decimal("1.00"), Decimal("0.00"))

        cancelled_confirmation = submit_confirmation(db, leave_fixture, start_offset=2, days=1)
        cancelled = submit_leave_request(
            db, leave_fixture.employee_user_id, cancelled_confirmation.id, uuid.uuid4()
        )
        cancel_intent = cancel_confirmation(db, leave_fixture, cancelled.id)
        cancelled_view = cancel_leave_request(
            db,
            leave_fixture.employee_user_id,
            cancelled.id,
            cancel_intent.id,
            uuid.uuid4(),
        )
        assert cancelled_view.status == LeaveRequestStatus.CANCELLED
        assert account_state(db, leave_fixture) == (Decimal("1.00"), Decimal("0.00"))


@pytest.mark.parametrize("transition", ["approve", "reject"])
def test_review_replay_requires_current_scope(
    leave_fixture: LeaveFixture,
    transition: str,
) -> None:
    with leave_fixture.sessions() as db:
        suffix = uuid.uuid4().hex
        unit_a = OrganizationUnit(code=f"TX-SCOPE-A-{suffix}", name="Scope A")
        unit_b = OrganizationUnit(code=f"TX-SCOPE-B-{suffix}", name="Scope B")
        db.add_all([unit_a, unit_b])
        db.flush()
        employee = db.get(EmployeeProfile, leave_fixture.employee_id)
        reviewer = db.get(User, leave_fixture.reviewer_user_id)
        assert employee is not None and reviewer is not None
        employee.organization_unit_id = unit_a.id
        grant_a = CapabilityGrant(
            user_id=reviewer.id,
            capability=Capability.HR_LEAVE_REVIEW.value,
            scope_kind=ScopeKind.UNIT_SUBTREE.value,
            organization_unit_id=unit_a.id,
            is_active=True,
        )
        db.add(grant_a)
        db.commit()
        scope_a, resolver = review_access(db, leave_fixture)
        confirmation = submit_confirmation(db, leave_fixture, days=1)
        request = submit_leave_request(
            db,
            leave_fixture.employee_user_id,
            confirmation.id,
            uuid.uuid4(),
        )
        operation_id = uuid.uuid4()
        if transition == "approve":
            approve_leave_request(
                db,
                reviewer.id,
                request.id,
                operation_id,
                review_scope=scope_a,
                capability_resolver=resolver,
            )
        else:
            reject_leave_request(
                db,
                reviewer.id,
                request.id,
                operation_id,
                "staffing conflict",
                review_scope=scope_a,
                capability_resolver=resolver,
            )

        grant_a.is_active = False
        db.add(
            CapabilityGrant(
                user_id=reviewer.id,
                capability=Capability.HR_LEAVE_REVIEW.value,
                scope_kind=ScopeKind.UNIT_SUBTREE.value,
                organization_unit_id=unit_b.id,
                is_active=True,
            )
        )
        db.commit()
        scope_b, resolver = review_access(db, leave_fixture)
        with pytest.raises(HrDomainError, match="leave_request_not_found"):
            if transition == "approve":
                approve_leave_request(
                    db,
                    reviewer.id,
                    request.id,
                    operation_id,
                    review_scope=scope_b,
                    capability_resolver=resolver,
                )
            else:
                reject_leave_request(
                    db,
                    reviewer.id,
                    request.id,
                    operation_id,
                    "staffing conflict",
                    review_scope=scope_b,
                    capability_resolver=resolver,
                )

        assert db.scalar(
            select(func.count()).select_from(LeaveAccountEvent).where(
                LeaveAccountEvent.operation_id == operation_id
            )
        ) == 1


@pytest.mark.parametrize("transition", ["approve", "reject"])
def test_review_rechecks_scope_after_lock_before_mutating_balance(
    leave_fixture: LeaveFixture,
    transition: str,
) -> None:
    with leave_fixture.sessions() as db:
        unit = OrganizationUnit(
            code=f"TX-SCOPE-{uuid.uuid4().hex}",
            name="Transaction Scope",
        )
        db.add(unit)
        db.flush()
        employee = db.get(EmployeeProfile, leave_fixture.employee_id)
        reviewer = db.get(User, leave_fixture.reviewer_user_id)
        assert employee is not None and reviewer is not None
        employee.organization_unit_id = unit.id
        grant = CapabilityGrant(
            user_id=reviewer.id,
            capability=Capability.HR_LEAVE_REVIEW.value,
            scope_kind=ScopeKind.UNIT_SUBTREE.value,
            organization_unit_id=unit.id,
            is_active=True,
        )
        db.add(grant)
        db.commit()

        confirmation = submit_confirmation(db, leave_fixture, days=1)
        request = submit_leave_request(
            db,
            leave_fixture.employee_user_id,
            confirmation.id,
            uuid.uuid4(),
        )
        resolver = CapabilityResolver()
        initial_scope = resolver.scope_for(
            db,
            reviewer,
            Capability.HR_LEAVE_REVIEW,
        )
        assert initial_scope is not None

        class RevokingResolver(CapabilityResolver):
            def scope_for(self, current_db, user, capability):  # type: ignore[no-untyped-def]
                with leave_fixture.sessions() as revoke_db:
                    stored = revoke_db.get(CapabilityGrant, grant.id)
                    assert stored is not None
                    stored.is_active = False
                    revoke_db.commit()
                return super().scope_for(current_db, user, capability)

        operation_id = uuid.uuid4()
        with pytest.raises(HrDomainError, match="leave_request_not_found"):
            if transition == "approve":
                approve_leave_request(
                    db,
                    reviewer.id,
                    request.id,
                    operation_id,
                    review_scope=initial_scope,
                    capability_resolver=RevokingResolver(),
                )
            else:
                reject_leave_request(
                    db,
                    reviewer.id,
                    request.id,
                    operation_id,
                    "scope revoked",
                    review_scope=initial_scope,
                    capability_resolver=RevokingResolver(),
                )

        db.expire_all()
        stored_request = db.get(LeaveRequest, request.id)
        assert stored_request is not None
        assert stored_request.status == LeaveRequestStatus.PENDING
        assert account_state(db, leave_fixture) == (
            Decimal("0.00"),
            Decimal("1.00"),
        )
        assert db.scalar(
            select(func.count()).select_from(LeaveAccountEvent).where(
                LeaveAccountEvent.operation_id == operation_id
            )
        ) == 0


def test_concurrent_approve_and_cancel_have_exactly_one_winner(
    leave_fixture: LeaveFixture,
) -> None:
    with leave_fixture.sessions() as db:
        grant_global_review(db, leave_fixture)
        submit_intent = submit_confirmation(db, leave_fixture, days=1)
        request = submit_leave_request(
            db, leave_fixture.employee_user_id, submit_intent.id, uuid.uuid4()
        )
        cancel_intent = cancel_confirmation(db, leave_fixture, request.id)
        request_id = request.id
        cancel_id = cancel_intent.id

    barrier = Barrier(2)

    def approve() -> str:
        with leave_fixture.sessions() as db:
            barrier.wait()
            try:
                review_scope, capability_resolver = review_access(
                    db,
                    leave_fixture,
                )
                return approve_leave_request(
                    db,
                    leave_fixture.reviewer_user_id,
                    request_id,
                    uuid.uuid4(),
                    review_scope=review_scope,
                    capability_resolver=capability_resolver,
                ).status.value
            except HrDomainError as exc:
                return exc.code

    def cancel() -> str:
        with leave_fixture.sessions() as db:
            barrier.wait()
            try:
                return cancel_leave_request(
                    db,
                    leave_fixture.employee_user_id,
                    request_id,
                    cancel_id,
                    uuid.uuid4(),
                ).status.value
            except HrDomainError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(approve), executor.submit(cancel)]
        outcomes = [future.result(timeout=10) for future in results]

    assert sorted(outcomes) in [
        ["approved", "leave_request_state_conflict"],
        ["cancelled", "leave_request_state_conflict"],
    ]
    with leave_fixture.sessions() as db:
        final_request = db.get(LeaveRequest, request_id)
        assert final_request is not None
        assert final_request.status in {
            LeaveRequestStatus.APPROVED,
            LeaveRequestStatus.CANCELLED,
        }
        expected = (
            (Decimal("1.00"), Decimal("0.00"))
            if final_request.status == LeaveRequestStatus.APPROVED
            else (Decimal("0.00"), Decimal("0.00"))
        )
        assert account_state(db, leave_fixture) == expected
