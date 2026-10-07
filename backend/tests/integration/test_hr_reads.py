from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from policy_api.database import assert_test_database_url
from policy_api.hr.enums import (
    LeaveRequestStatus,
    LeaveTypeCode,
    WorkCalendarDayKind,
)
from policy_api.hr.models import (
    EmployeeProfile,
    LeaveAccount,
    LeaveRequest,
    LeaveType,
    WorkCalendarDay,
)
from policy_api.hr.schemas import HrDomainError
from policy_api.hr.service import (
    get_my_leave_balances,
    get_my_leave_request,
    get_leave_duration,
    list_my_leave_requests,
)
from policy_api.models import User, UserRole


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
def hr_read_session() -> tuple[Session, dict[str, object]]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for HR read integration tests")
    assert_test_database_url(TEST_DATABASE_URL)
    backend_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(backend_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_root / "alembic"))
    original_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    command.upgrade(alembic_config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    suffix = uuid.uuid4().hex
    try:
        alice_user = User(
            username=f"alice-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        bob_user = User(
            username=f"bob-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        session.add_all([alice_user, bob_user])
        session.flush()
        alice = EmployeeProfile(
            user_id=alice_user.id,
            employee_number=f"A-{suffix}",
            display_name="Alice",
            hire_date=date(2024, 1, 1),
            is_active=True,
        )
        bob = EmployeeProfile(
            user_id=bob_user.id,
            employee_number=f"B-{suffix}",
            display_name="Bob",
            hire_date=date(2024, 1, 1),
            is_active=True,
        )
        annual = LeaveType(
            code=LeaveTypeCode.ANNUAL,
            display_name="Annual leave",
            is_enabled=True,
        )
        session.add_all([alice, bob, annual])
        session.flush()
        session.add_all(
            [
                LeaveAccount(
                    employee_id=alice.id,
                    leave_type_id=annual.id,
                    year=2026,
                    entitled=Decimal("10.00"),
                    used=Decimal("2.00"),
                    reserved=Decimal("1.50"),
                    version=1,
                ),
                LeaveAccount(
                    employee_id=bob.id,
                    leave_type_id=annual.id,
                    year=2026,
                    entitled=Decimal("20.00"),
                    used=Decimal("1.00"),
                    reserved=Decimal("0.00"),
                    version=1,
                ),
            ]
        )
        alice_request = LeaveRequest(
            request_number=f"LR-A-{suffix}",
            employee_id=alice.id,
            leave_type_id=annual.id,
            start_date=date(2026, 8, 17),
            end_date=date(2026, 8, 18),
            workday_count=Decimal("2.00"),
            reason="Alice request",
            status=LeaveRequestStatus.PENDING,
            submitted_at=now,
        )
        bob_request = LeaveRequest(
            request_number=f"LR-B-{suffix}",
            employee_id=bob.id,
            leave_type_id=annual.id,
            start_date=date(2026, 8, 19),
            end_date=date(2026, 8, 19),
            workday_count=Decimal("1.00"),
            reason="Bob request",
            status=LeaveRequestStatus.PENDING,
            submitted_at=now,
        )
        session.add_all([alice_request, bob_request])
        session.add_all(
            [
                WorkCalendarDay(
                    calendar_date=date(2026, 8, 17),
                    kind=WorkCalendarDayKind.WORKDAY,
                    is_workday=True,
                    label=None,
                ),
                WorkCalendarDay(
                    calendar_date=date(2026, 8, 18),
                    kind=WorkCalendarDayKind.HOLIDAY,
                    is_workday=False,
                    label="Holiday",
                ),
                WorkCalendarDay(
                    calendar_date=date(2026, 8, 19),
                    kind=WorkCalendarDayKind.ADJUSTED_WORKDAY,
                    is_workday=True,
                    label="Adjusted workday",
                ),
            ]
        )
        session.flush()
        yield session, {
            "alice_user_id": alice_user.id,
            "bob_user_id": bob_user.id,
            "alice_request_id": alice_request.id,
            "bob_request_id": bob_request.id,
        }
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url


def test_balances_are_owner_scoped_and_compute_available_server_side(
    hr_read_session: tuple[Session, dict[str, object]],
) -> None:
    session, ids = hr_read_session

    balances = get_my_leave_balances(session, ids["alice_user_id"], year=2026)

    assert len(balances) == 1
    assert balances[0].leave_type_code == LeaveTypeCode.ANNUAL
    assert balances[0].entitled == Decimal("10.00")
    assert balances[0].used == Decimal("2.00")
    assert balances[0].reserved == Decimal("1.50")
    assert balances[0].available == Decimal("6.50")


def test_requests_are_owner_scoped_and_foreign_matches_missing(
    hr_read_session: tuple[Session, dict[str, object]],
) -> None:
    session, ids = hr_read_session

    requests = list_my_leave_requests(session, ids["alice_user_id"])
    assert [item.id for item in requests] == [ids["alice_request_id"]]
    assert get_my_leave_request(
        session, ids["alice_user_id"], ids["alice_request_id"]
    ).reason == "Alice request"

    for hidden_id in (ids["bob_request_id"], uuid.uuid4()):
        with pytest.raises(HrDomainError, match="leave_request_not_found"):
            get_my_leave_request(session, ids["alice_user_id"], hidden_id)


def test_duration_service_loads_the_inclusive_calendar_from_postgresql(
    hr_read_session: tuple[Session, dict[str, object]],
) -> None:
    session, _ids = hr_read_session

    duration = get_leave_duration(session, date(2026, 8, 17), date(2026, 8, 19))

    assert duration.workday_count == Decimal("2")
