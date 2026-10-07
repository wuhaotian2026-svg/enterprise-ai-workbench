from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from policy_api.auth.passwords import hash_password
from policy_api.database import (
    assert_test_database_url,
    create_database_engine,
    create_session_factory,
)
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
from policy_api.models import User, UserRole
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    OrganizationUnit,
    ScopeKind,
)


DEMO_CALENDAR_START = date(2026, 8, 1)
DEMO_CALENDAR_END = date(2026, 10, 31)
DEMO_REQUEST_NUMBERS = (
    "DEMO-LR-APPROVED-001",
    "DEMO-LR-REJECTED-001",
    "DEMO-LR-PENDING-001",
)
DEMO_ORGANIZATION_SPECS = (
    ("EAIW-DEMO", "企业 AI 工作台虚构企业"),
    ("EAIW-DEMO-PRODUCT", "产品中心"),
    ("EAIW-DEMO-PEOPLE", "人力中心"),
)


def _new_summary() -> dict[str, int]:
    return {"created": 0, "updated": 0, "unchanged": 0}


def _record(summary: dict[str, int], outcome: str) -> None:
    summary[outcome] += 1


def _update_fields(instance: object, values: Mapping[str, object]) -> str:
    changed = False
    for name, value in values.items():
        if getattr(instance, name) != value:
            setattr(instance, name, value)
            changed = True
    return "updated" if changed else "unchanged"


def _ensure_user(
    db: Session,
    *,
    username: str,
    password: str,
    role: UserRole,
) -> tuple[User, str]:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            is_active=True,
        )
        db.add(user)
        db.flush()
        return user, "created"
    if user.role != role or not user.is_active:
        raise SystemExit(
            "Demo user identity conflicts with the expected role or active status."
        )
    # Existing demo identities are never mutated, including their password hashes.
    return user, "unchanged"


def _ensure_organization(
    db: Session,
    *,
    code: str,
    name: str,
    parent_id: uuid.UUID | None,
) -> tuple[OrganizationUnit, str]:
    unit = db.scalar(select(OrganizationUnit).where(OrganizationUnit.code == code))
    if unit is None:
        unit = OrganizationUnit(
            code=code,
            name=name,
            parent_id=parent_id,
            is_active=True,
        )
        db.add(unit)
        db.flush()
        return unit, "created"
    if (unit.name, unit.parent_id, unit.is_active) != (name, parent_id, True):
        raise SystemExit(
            f"Demo organization code {code!r} is owned by another shape."
        )
    return unit, "unchanged"


def _ensure_grant(
    db: Session,
    *,
    user: User,
    capability: Capability,
    scope_kind: ScopeKind,
    organization_unit_id: uuid.UUID | None,
) -> str:
    grants = list(
        db.scalars(
            select(CapabilityGrant).where(
                CapabilityGrant.user_id == user.id,
                CapabilityGrant.capability == capability.value,
            )
        )
    )
    matching = [
        grant
        for grant in grants
        if grant.scope_kind == scope_kind.value
        and grant.organization_unit_id == organization_unit_id
        and grant.is_active
    ]
    if not grants:
        db.add(
            CapabilityGrant(
                user_id=user.id,
                capability=capability.value,
                scope_kind=scope_kind.value,
                organization_unit_id=organization_unit_id,
                is_active=True,
            )
        )
        db.flush()
        return "created"
    if len(grants) == 1 and len(matching) == 1:
        return "unchanged"
    raise SystemExit(
        "Demo capability grant conflicts with existing scope or inactive history."
    )


def _ensure_employee(
    db: Session,
    alice: User,
    product_unit: OrganizationUnit,
    manager: EmployeeProfile,
) -> tuple[EmployeeProfile, str]:
    by_number = db.scalar(
        select(EmployeeProfile).where(
            EmployeeProfile.employee_number == "DEMO-A001"
        )
    )
    by_user = db.scalar(
        select(EmployeeProfile).where(EmployeeProfile.user_id == alice.id)
    )
    if by_number is not None and by_number.user_id != alice.id:
        raise SystemExit("Demo employee number is already owned by another user.")
    if by_user is not None and by_user.employee_number != "DEMO-A001":
        raise SystemExit("Demo user is already linked to another employee profile.")
    employee = by_number or by_user
    if (
        employee is not None
        and employee.organization_unit_id is not None
        and employee.organization_unit_id != product_unit.id
    ):
        raise SystemExit(
            "Demo employee is already assigned to another organization."
        )
    if (
        employee is not None
        and employee.manager_employee_id is not None
        and employee.manager_employee_id != manager.id
    ):
        raise SystemExit("Demo employee profile conflicts with the exact manager.")
    values: dict[str, object] = {
        "user_id": alice.id,
        "employee_number": "DEMO-A001",
        "display_name": "Alice（虚构演示员工）",
        "department": "产品与技术中心",
        "organization_unit_id": product_unit.id,
        "manager_employee_id": manager.id,
        "hire_date": date(2024, 1, 8),
        "is_active": True,
    }
    if employee is None:
        employee = EmployeeProfile(**values)  # type: ignore[arg-type]
        db.add(employee)
        db.flush()
        return employee, "created"
    return employee, _update_fields(employee, values)


def _ensure_procurement_reviewer_profile(
    db: Session,
    *,
    user: User,
    employee_number: str,
    display_name: str,
    department: str,
    organization_unit: OrganizationUnit,
    hire_date: date,
) -> tuple[EmployeeProfile, str]:
    by_number = db.scalar(
        select(EmployeeProfile).where(
            EmployeeProfile.employee_number == employee_number
        )
    )
    by_user = db.scalar(
        select(EmployeeProfile).where(EmployeeProfile.user_id == user.id)
    )
    if by_number is not None and by_number.user_id != user.id:
        raise SystemExit("Demo employee number is already owned by another user.")
    if by_user is not None and by_user.employee_number != employee_number:
        raise SystemExit("Demo user is already linked to another employee profile.")
    employee = by_number or by_user
    values: dict[str, object] = {
        "user_id": user.id,
        "employee_number": employee_number,
        "display_name": display_name,
        "department": department,
        "organization_unit_id": organization_unit.id,
        "manager_employee_id": None,
        "hire_date": hire_date,
        "is_active": True,
    }
    if employee is None:
        employee = EmployeeProfile(**values)  # type: ignore[arg-type]
        db.add(employee)
        db.flush()
        return employee, "created"
    if any(getattr(employee, name) != value for name, value in values.items()):
        raise SystemExit("Demo employee profile conflicts with the expected shape.")
    return employee, "unchanged"


def _ensure_leave_type(
    db: Session, code: LeaveTypeCode, display_name: str
) -> tuple[LeaveType, str]:
    leave_type = db.scalar(select(LeaveType).where(LeaveType.code == code))
    if leave_type is None:
        leave_type = LeaveType(
            code=code, display_name=display_name, is_enabled=True
        )
        db.add(leave_type)
        db.flush()
        return leave_type, "created"
    # Leave types are shared domain data. Existing rows are never overwritten by demo seed.
    return leave_type, "unchanged"


def _ensure_account(
    db: Session,
    *,
    employee: EmployeeProfile,
    leave_type: LeaveType,
    entitled: Decimal,
    used: Decimal,
    reserved: Decimal,
) -> str:
    account = db.scalar(
        select(LeaveAccount).where(
            LeaveAccount.employee_id == employee.id,
            LeaveAccount.leave_type_id == leave_type.id,
            LeaveAccount.year == 2026,
        )
    )
    values = {
        "entitled": entitled,
        "used": used,
        "reserved": reserved,
    }
    if account is None:
        db.add(
            LeaveAccount(
                employee_id=employee.id,
                leave_type_id=leave_type.id,
                year=2026,
                version=1,
                **values,
            )
        )
        return "created"
    return _update_fields(account, values)


def _ensure_calendar(db: Session, summary: dict[str, int]) -> None:
    existing = set(
        db.scalars(
            select(WorkCalendarDay.calendar_date).where(
                WorkCalendarDay.calendar_date.between(
                    DEMO_CALENDAR_START, DEMO_CALENDAR_END
                )
            )
        )
    )
    current = DEMO_CALENDAR_START
    while current <= DEMO_CALENDAR_END:
        if current in existing:
            _record(summary, "unchanged")
        else:
            is_workday = current.weekday() < 5
            db.add(
                WorkCalendarDay(
                    calendar_date=current,
                    kind=(
                        WorkCalendarDayKind.WORKDAY
                        if is_workday
                        else WorkCalendarDayKind.WEEKEND
                    ),
                    is_workday=is_workday,
                    label=None,
                )
            )
            _record(summary, "created")
        current += timedelta(days=1)
    db.flush()


def _ensure_request(
    db: Session,
    *,
    request_number: str,
    employee: EmployeeProfile,
    leave_type: LeaveType,
    values: Mapping[str, object],
) -> str:
    request = db.scalar(
        select(LeaveRequest).where(
            LeaveRequest.request_number == request_number
        )
    )
    if request is not None and request.employee_id != employee.id:
        raise SystemExit("Demo request number is already owned by another employee.")
    owned_values = {
        "employee_id": employee.id,
        "leave_type_id": leave_type.id,
        **values,
    }
    if request is None:
        db.add(LeaveRequest(request_number=request_number, **owned_values))  # type: ignore[arg-type]
        return "created"
    return _update_fields(request, owned_values)


def seed_hr_demo(
    db: Session,
    *,
    alice_password: str,
    helen_password: str,
    admin_password: str,
    manager_password: str,
    specialist_password: str,
) -> dict[str, int]:
    summary = _new_summary()
    users: dict[str, User] = {}
    for key, username, password, role in (
        ("alice", "alice.hr.demo", alice_password, UserRole.EMPLOYEE),
        ("helen", "helen.hr.demo", helen_password, UserRole.HR),
        ("admin", "admin.hr.demo", admin_password, UserRole.ADMIN),
        (
            "manager",
            "manager.procurement.demo",
            manager_password,
            UserRole.EMPLOYEE,
        ),
        (
            "specialist",
            "specialist.procurement.demo",
            specialist_password,
            UserRole.EMPLOYEE,
        ),
    ):
        users[key], outcome = _ensure_user(
            db, username=username, password=password, role=role
        )
        _record(summary, outcome)

    root, outcome = _ensure_organization(
        db,
        code=DEMO_ORGANIZATION_SPECS[0][0],
        name=DEMO_ORGANIZATION_SPECS[0][1],
        parent_id=None,
    )
    _record(summary, outcome)
    product, outcome = _ensure_organization(
        db,
        code=DEMO_ORGANIZATION_SPECS[1][0],
        name=DEMO_ORGANIZATION_SPECS[1][1],
        parent_id=root.id,
    )
    _record(summary, outcome)
    _, outcome = _ensure_organization(
        db,
        code=DEMO_ORGANIZATION_SPECS[2][0],
        name=DEMO_ORGANIZATION_SPECS[2][1],
        parent_id=root.id,
    )
    _record(summary, outcome)

    manager_profile, outcome = _ensure_procurement_reviewer_profile(
        db,
        user=users["manager"],
        employee_number="DEMO-M001",
        display_name="Ming（虚构直属部门负责人）",
        department="产品与技术中心",
        organization_unit=product,
        hire_date=date(2022, 5, 9),
    )
    _record(summary, outcome)
    _, outcome = _ensure_procurement_reviewer_profile(
        db,
        user=users["specialist"],
        employee_number="DEMO-P001",
        display_name="Penny（虚构采购专员）",
        department="采购运营",
        organization_unit=product,
        hire_date=date(2023, 3, 6),
    )
    _record(summary, outcome)
    employee, outcome = _ensure_employee(
        db, users["alice"], product, manager_profile
    )
    _record(summary, outcome)
    _record(
        summary,
        _ensure_grant(
            db,
            user=users["helen"],
            capability=Capability.HR_LEAVE_REVIEW,
            scope_kind=ScopeKind.UNIT_SUBTREE,
            organization_unit_id=product.id,
        ),
    )
    for user_key, capabilities in (
        (
            "manager",
            (
                Capability.APPROVAL_INBOX_VIEW,
                Capability.PROCUREMENT_DEPARTMENT_REVIEW,
            ),
        ),
        (
            "specialist",
            (
                Capability.APPROVAL_INBOX_VIEW,
                Capability.PROCUREMENT_FINAL_REVIEW,
            ),
        ),
    ):
        for capability in capabilities:
            _record(
                summary,
                _ensure_grant(
                    db,
                    user=users[user_key],
                    capability=capability,
                    scope_kind=ScopeKind.UNIT_SUBTREE,
                    organization_unit_id=product.id,
                ),
            )
    for capability in (
        Capability.KNOWLEDGE_MANAGE,
        Capability.ORGANIZATION_MANAGE,
        Capability.ANALYTICS_VIEW,
    ):
        _record(
            summary,
            _ensure_grant(
                db,
                user=users["admin"],
                capability=capability,
                scope_kind=ScopeKind.GLOBAL,
                organization_unit_id=None,
            ),
        )
    annual, outcome = _ensure_leave_type(
        db, LeaveTypeCode.ANNUAL, "年假"
    )
    _record(summary, outcome)
    compensatory, outcome = _ensure_leave_type(
        db, LeaveTypeCode.COMPENSATORY, "调休"
    )
    _record(summary, outcome)
    _record(
        summary,
        _ensure_account(
            db,
            employee=employee,
            leave_type=annual,
            entitled=Decimal("10.00"),
            used=Decimal("2.00"),
            reserved=Decimal("1.00"),
        ),
    )
    _record(
        summary,
        _ensure_account(
            db,
            employee=employee,
            leave_type=compensatory,
            entitled=Decimal("4.00"),
            used=Decimal("0.00"),
            reserved=Decimal("0.00"),
        ),
    )
    _ensure_calendar(db, summary)

    submitted = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    reviewed = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
    request_specs: tuple[tuple[str, Mapping[str, object]], ...] = (
        (
            DEMO_REQUEST_NUMBERS[0],
            {
                "start_date": date(2026, 8, 3),
                "end_date": date(2026, 8, 4),
                "workday_count": Decimal("2.00"),
                "reason": "虚构演示：家庭事务",
                "status": LeaveRequestStatus.APPROVED,
                "submitted_at": submitted,
                "reviewer_user_id": users["helen"].id,
                "reviewed_at": reviewed,
                "rejection_reason": None,
                "cancelled_at": None,
                "cancelled_by_user_id": None,
            },
        ),
        (
            DEMO_REQUEST_NUMBERS[1],
            {
                "start_date": date(2026, 8, 10),
                "end_date": date(2026, 8, 10),
                "workday_count": Decimal("1.00"),
                "reason": "虚构演示：个人事务",
                "status": LeaveRequestStatus.REJECTED,
                "submitted_at": submitted,
                "reviewer_user_id": users["helen"].id,
                "reviewed_at": reviewed,
                "rejection_reason": "虚构演示：排班冲突",
                "cancelled_at": None,
                "cancelled_by_user_id": None,
            },
        ),
        (
            DEMO_REQUEST_NUMBERS[2],
            {
                "start_date": date(2026, 8, 17),
                "end_date": date(2026, 8, 17),
                "workday_count": Decimal("1.00"),
                "reason": "虚构演示：待审批申请",
                "status": LeaveRequestStatus.PENDING,
                "submitted_at": submitted,
                "reviewer_user_id": None,
                "reviewed_at": None,
                "rejection_reason": None,
                "cancelled_at": None,
                "cancelled_by_user_id": None,
            },
        ),
    )
    for request_number, values in request_specs:
        _record(
            summary,
            _ensure_request(
                db,
                request_number=request_number,
                employee=employee,
                leave_type=annual,
                values=values,
            ),
        )
    db.flush()
    return summary


def validate_database_target(
    database_url: str, *, allow_non_test: bool
) -> None:
    try:
        assert_test_database_url(database_url)
    except ValueError:
        if not allow_non_test:
            raise SystemExit(
                "Non-test demo database requires --allow-non-test-demo-database."
            ) from None


def _required_password(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


def print_summary(summary: Mapping[str, int]) -> None:
    for key in ("created", "updated", "unchanged"):
        print(f"{key}={summary[key]}")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed fictional HR demo data.")
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--allow-non-test-demo-database", action="store_true"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    validate_database_target(
        args.database_url,
        allow_non_test=args.allow_non_test_demo_database,
    )
    engine = create_database_engine(args.database_url)
    sessions = create_session_factory(engine)
    try:
        with sessions.begin() as db:
            summary = seed_hr_demo(
                db,
                alice_password=_required_password(
                    os.environ, "HR_DEMO_ALICE_PASSWORD"
                ),
                helen_password=_required_password(
                    os.environ, "HR_DEMO_HELEN_PASSWORD"
                ),
                admin_password=_required_password(
                    os.environ, "HR_DEMO_ADMIN_PASSWORD"
                ),
                manager_password=_required_password(
                    os.environ, "PROCUREMENT_DEMO_MANAGER_PASSWORD"
                ),
                specialist_password=_required_password(
                    os.environ, "PROCUREMENT_DEMO_SPECIALIST_PASSWORD"
                ),
            )
        print_summary(summary)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
