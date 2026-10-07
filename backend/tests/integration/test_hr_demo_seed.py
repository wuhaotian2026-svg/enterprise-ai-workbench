from __future__ import annotations

from datetime import date
import importlib.util
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from policy_api.auth.passwords import hash_password
from policy_api.database import assert_test_database_url
from policy_api.hr.enums import LeaveRequestStatus, LeaveTypeCode
from policy_api.hr.models import (
    EmployeeProfile,
    LeaveAccount,
    LeaveRequest,
    LeaveType,
    WorkCalendarDay,
)
from policy_api.models import Document, DocumentStatus, User, UserRole
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    OrganizationUnit,
    ScopeKind,
)


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "seed_hr_demo.py"
DEMO_USERNAMES = (
    "alice.hr.demo",
    "helen.hr.demo",
    "admin.hr.demo",
    "manager.procurement.demo",
    "specialist.procurement.demo",
)


def demo_passwords(*, rotated: bool = False) -> dict[str, str]:
    marker = "ROTATED" if rotated else "SECRET"
    return {
        "alice_password": f"ALICE-{marker}-MARKER",
        "helen_password": f"HELEN-{marker}-MARKER",
        "admin_password": f"ADMIN-{marker}-MARKER",
        "manager_password": f"MANAGER-{marker}-MARKER",
        "specialist_password": f"SPECIALIST-{marker}-MARKER",
    }


def load_seed_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("seed_hr_demo", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seed_session() -> tuple[Session, User, Document]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for HR seed integration tests")
    assert_test_database_url(TEST_DATABASE_URL)
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    command.upgrade(config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    try:
        unknown = User(
            username=f"unknown-seed-{suffix}",
            password_hash="unknown-password-hash",
            role=UserRole.EMPLOYEE,
            is_active=False,
        )
        document = Document(
            display_name="Unknown seed sentinel",
            storage_key=f"unknown/{suffix}.txt",
            sha256=(suffix * 2)[:64],
            mime_type="text/plain",
            status=DocumentStatus.ENABLED,
            is_enabled=True,
        )
        session.add_all([unknown, document])
        session.flush()
        yield session, unknown, document
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        engine.dispose()
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def test_seed_is_idempotent_non_destructive_and_redacts_passwords(
    seed_session: tuple[Session, User, Document], capsys
) -> None:
    db, unknown, document = seed_session
    seed = load_seed_module()
    passwords = demo_passwords()
    rotated_passwords = demo_passwords(rotated=True)
    unrelated_unit = OrganizationUnit(
        code=f"REAL-{uuid.uuid4().hex[:12].upper()}",
        name="Unrelated real organization",
        is_active=False,
    )
    unrelated_grant = CapabilityGrant(
        user_id=unknown.id,
        capability=Capability.ANALYTICS_VIEW.value,
        scope_kind=ScopeKind.GLOBAL.value,
        organization_unit_id=None,
        is_active=True,
    )
    db.add_all([unrelated_unit, unrelated_grant])
    db.flush()

    first = seed.seed_hr_demo(db, **passwords)
    password_hashes = {
        user.username: user.password_hash
        for user in db.scalars(
            select(User).where(
                User.username.in_(
                    DEMO_USERNAMES
                )
            )
        )
    }
    second = seed.seed_hr_demo(db, **rotated_passwords)
    seed.print_summary(second)
    output = capsys.readouterr().out

    assert first["created"] > 0
    assert second["created"] == 0
    assert second["updated"] == 0
    assert set(output.splitlines()) == {
        f"created={second['created']}",
        f"updated={second['updated']}",
        f"unchanged={second['unchanged']}",
    }
    assert not any(
        secret in output
        for secret in (*passwords.values(), *rotated_passwords.values())
    )

    demo_users = db.scalars(
        select(User).where(
            User.username.in_(
                DEMO_USERNAMES
            )
        )
    ).all()
    assert {(item.username, item.role) for item in demo_users} == {
        ("alice.hr.demo", UserRole.EMPLOYEE),
        ("helen.hr.demo", UserRole.HR),
        ("admin.hr.demo", UserRole.ADMIN),
        ("manager.procurement.demo", UserRole.EMPLOYEE),
        ("specialist.procurement.demo", UserRole.EMPLOYEE),
    }
    assert {
        item.username: item.password_hash for item in demo_users
    } == password_hashes
    alice = next(item for item in demo_users if item.username == "alice.hr.demo")
    helen = next(item for item in demo_users if item.username == "helen.hr.demo")
    admin = next(item for item in demo_users if item.username == "admin.hr.demo")
    manager = next(
        item for item in demo_users if item.username == "manager.procurement.demo"
    )
    specialist = next(
        item for item in demo_users if item.username == "specialist.procurement.demo"
    )
    units = {
        item.code: item
        for item in db.scalars(
            select(OrganizationUnit).where(
                OrganizationUnit.code.in_(
                    ("EAIW-DEMO", "EAIW-DEMO-PRODUCT", "EAIW-DEMO-PEOPLE")
                )
            )
        )
    }
    assert set(units) == {
        "EAIW-DEMO",
        "EAIW-DEMO-PRODUCT",
        "EAIW-DEMO-PEOPLE",
    }
    assert units["EAIW-DEMO"].name == "企业 AI 工作台虚构企业"
    assert units["EAIW-DEMO-PRODUCT"].parent_id == units["EAIW-DEMO"].id
    assert units["EAIW-DEMO-PEOPLE"].parent_id == units["EAIW-DEMO"].id
    profiles = {
        item.user_id: item
        for item in db.scalars(
            select(EmployeeProfile).where(
                EmployeeProfile.user_id.in_((alice.id, manager.id, specialist.id))
            )
        )
    }
    assert set(profiles) == {alice.id, manager.id, specialist.id}
    assert profiles[alice.id].employee_number == "DEMO-A001"
    assert profiles[manager.id].employee_number == "DEMO-M001"
    assert profiles[specialist.id].employee_number == "DEMO-P001"
    assert profiles[alice.id].organization_unit_id == units["EAIW-DEMO-PRODUCT"].id
    assert profiles[alice.id].manager_employee_id == profiles[manager.id].id
    assert profiles[manager.id].organization_unit_id == units["EAIW-DEMO-PRODUCT"].id
    assert profiles[specialist.id].organization_unit_id == units["EAIW-DEMO-PRODUCT"].id
    grants = {
        (
            item.user_id,
            item.capability,
            item.scope_kind,
            item.organization_unit_id,
            item.is_active,
        )
        for item in db.scalars(
            select(CapabilityGrant).where(
                CapabilityGrant.user_id.in_((helen.id, admin.id, manager.id, specialist.id))
            )
        )
    }
    assert grants == {
        (
            helen.id,
            Capability.HR_LEAVE_REVIEW.value,
            ScopeKind.UNIT_SUBTREE.value,
            units["EAIW-DEMO-PRODUCT"].id,
            True,
        ),
        (
            manager.id,
            Capability.APPROVAL_INBOX_VIEW.value,
            ScopeKind.UNIT_SUBTREE.value,
            units["EAIW-DEMO-PRODUCT"].id,
            True,
        ),
        (
            manager.id,
            Capability.PROCUREMENT_DEPARTMENT_REVIEW.value,
            ScopeKind.UNIT_SUBTREE.value,
            units["EAIW-DEMO-PRODUCT"].id,
            True,
        ),
        (
            specialist.id,
            Capability.APPROVAL_INBOX_VIEW.value,
            ScopeKind.UNIT_SUBTREE.value,
            units["EAIW-DEMO-PRODUCT"].id,
            True,
        ),
        (
            specialist.id,
            Capability.PROCUREMENT_FINAL_REVIEW.value,
            ScopeKind.UNIT_SUBTREE.value,
            units["EAIW-DEMO-PRODUCT"].id,
            True,
        ),
        *{
            (
                admin.id,
                capability.value,
                ScopeKind.GLOBAL.value,
                None,
                True,
            )
            for capability in (
                Capability.KNOWLEDGE_MANAGE,
                Capability.ORGANIZATION_MANAGE,
                Capability.ANALYTICS_VIEW,
            )
        },
    }
    assert db.scalar(select(func.count()).select_from(LeaveType)) >= 2
    assert set(
        db.scalars(
            select(LeaveType.code).where(
                LeaveType.code.in_(
                    (LeaveTypeCode.ANNUAL, LeaveTypeCode.COMPENSATORY)
                )
            )
        )
    ) == {LeaveTypeCode.ANNUAL, LeaveTypeCode.COMPENSATORY}
    assert db.scalar(
        select(func.count())
        .select_from(LeaveAccount)
        .where(LeaveAccount.employee_id == profiles[alice.id].id)
    ) == 2

    expected_days = (seed.DEMO_CALENDAR_END - seed.DEMO_CALENDAR_START).days + 1
    assert db.scalar(
        select(func.count())
        .select_from(WorkCalendarDay)
        .where(
            WorkCalendarDay.calendar_date.between(
                seed.DEMO_CALENDAR_START, seed.DEMO_CALENDAR_END
            )
        )
    ) == expected_days
    requests = db.scalars(
        select(LeaveRequest).where(
            LeaveRequest.request_number.in_(seed.DEMO_REQUEST_NUMBERS)
        )
    ).all()
    assert len(requests) == 3
    assert {item.status for item in requests} == {
        LeaveRequestStatus.PENDING,
        LeaveRequestStatus.APPROVED,
        LeaveRequestStatus.REJECTED,
    }

    db.refresh(unknown)
    db.refresh(document)
    db.refresh(unrelated_unit)
    db.refresh(unrelated_grant)
    assert unknown.password_hash == "unknown-password-hash"
    assert unknown.role == UserRole.EMPLOYEE and unknown.is_active is False
    assert document.display_name == "Unknown seed sentinel"
    assert document.status == DocumentStatus.ENABLED and document.is_enabled is True
    assert unrelated_unit.name == "Unrelated real organization"
    assert unrelated_unit.is_active is False
    assert unrelated_grant.user_id == unknown.id
    assert unrelated_grant.is_active is True


def test_seed_fails_closed_when_demo_organization_code_is_owned_by_another_shape(
    seed_session: tuple[Session, User, Document],
) -> None:
    db, _, _ = seed_session
    seed = load_seed_module()
    conflicting = OrganizationUnit(
        code="EAIW-DEMO",
        name="Existing real organization",
        is_active=True,
    )
    db.add(conflicting)
    db.flush()

    with pytest.raises(SystemExit, match="Demo organization code"):
        with db.begin_nested():
            seed.seed_hr_demo(
                db,
                **demo_passwords(),
            )

    db.refresh(conflicting)
    assert conflicting.name == "Existing real organization"


@pytest.mark.parametrize(
    ("role", "is_active"),
    (
        (UserRole.ADMIN, True),
        (UserRole.EMPLOYEE, False),
    ),
)
def test_seed_fails_closed_when_demo_username_has_an_incompatible_identity(
    seed_session: tuple[Session, User, Document],
    role: UserRole,
    is_active: bool,
) -> None:
    db, _, _ = seed_session
    seed = load_seed_module()
    conflicting = User(
        username="alice.hr.demo",
        password_hash=hash_password("ALICE-SECRET-MARKER"),
        role=role,
        is_active=is_active,
    )
    db.add(conflicting)
    db.flush()

    with pytest.raises(SystemExit, match="Demo user identity"):
        with db.begin_nested():
            seed.seed_hr_demo(
                db,
                **demo_passwords(),
            )

    db.refresh(conflicting)
    assert conflicting.role == role
    assert conflicting.is_active is is_active


def test_seed_fails_closed_when_demo_employee_belongs_to_another_organization(
    seed_session: tuple[Session, User, Document],
) -> None:
    db, _, _ = seed_session
    seed = load_seed_module()
    passwords = demo_passwords()
    seed.seed_hr_demo(db, **passwords)
    alice = db.scalar(select(User).where(User.username == "alice.hr.demo"))
    assert alice is not None
    employee = db.scalar(
        select(EmployeeProfile).where(EmployeeProfile.user_id == alice.id)
    )
    assert employee is not None
    unrelated = OrganizationUnit(
        code=f"REAL-{uuid.uuid4().hex[:12].upper()}",
        name="Existing employee organization",
        is_active=True,
    )
    db.add(unrelated)
    db.flush()
    employee.organization_unit_id = unrelated.id
    db.flush()

    with pytest.raises(SystemExit, match="Demo employee"):
        with db.begin_nested():
            seed.seed_hr_demo(db, **passwords)

    db.refresh(employee)
    assert employee.organization_unit_id == unrelated.id


def test_seed_fails_closed_instead_of_replacing_alices_exact_manager(
    seed_session: tuple[Session, User, Document],
) -> None:
    db, _, _ = seed_session
    seed = load_seed_module()
    passwords = demo_passwords()
    seed.seed_hr_demo(db, **passwords)
    alice = db.scalar(select(User).where(User.username == "alice.hr.demo"))
    assert alice is not None
    alice_profile = db.scalar(
        select(EmployeeProfile).where(EmployeeProfile.user_id == alice.id)
    )
    assert alice_profile is not None
    original_manager_id = alice_profile.manager_employee_id
    alternative_user = User(
        username=f"alternative-manager-{uuid.uuid4().hex}",
        password_hash="alternative-manager-hash",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )
    db.add(alternative_user)
    db.flush()
    alternative_manager = EmployeeProfile(
        user_id=alternative_user.id,
        employee_number=f"ALT-{uuid.uuid4().hex[:12].upper()}",
        display_name="Alternative manager",
        department="Product",
        organization_unit_id=alice_profile.organization_unit_id,
        manager_employee_id=None,
        hire_date=date(2020, 1, 1),
        is_active=True,
    )
    db.add(alternative_manager)
    db.flush()
    alice_profile.manager_employee_id = alternative_manager.id
    db.flush()

    with pytest.raises(SystemExit, match="Demo employee profile"):
        with db.begin_nested():
            seed.seed_hr_demo(db, **passwords)

    db.refresh(alice_profile)
    assert original_manager_id is not None
    assert alice_profile.manager_employee_id == alternative_manager.id


def test_seed_fails_closed_for_conflicting_procurement_reviewer_profile(
    seed_session: tuple[Session, User, Document],
) -> None:
    db, _, _ = seed_session
    seed = load_seed_module()
    passwords = demo_passwords()
    seed.seed_hr_demo(db, **passwords)
    manager = db.scalar(
        select(User).where(User.username == "manager.procurement.demo")
    )
    assert manager is not None
    manager_profile = db.scalar(
        select(EmployeeProfile).where(EmployeeProfile.user_id == manager.id)
    )
    assert manager_profile is not None
    unrelated = OrganizationUnit(
        code=f"REAL-{uuid.uuid4().hex[:12].upper()}",
        name="Conflicting reviewer organization",
        is_active=True,
    )
    db.add(unrelated)
    db.flush()
    manager_profile.organization_unit_id = unrelated.id
    db.flush()

    with pytest.raises(SystemExit, match="Demo employee profile"):
        with db.begin_nested():
            seed.seed_hr_demo(db, **passwords)

    db.refresh(manager_profile)
    assert manager_profile.organization_unit_id == unrelated.id


def test_seed_caller_transaction_rolls_back_all_demo_resources_on_failure(
    seed_session: tuple[Session, User, Document], monkeypatch
) -> None:
    db, _, _ = seed_session
    seed = load_seed_module()

    def fail_account(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected seed failure")

    monkeypatch.setattr(seed, "_ensure_account", fail_account)
    with pytest.raises(RuntimeError, match="injected seed failure"):
        with db.begin_nested():
            seed.seed_hr_demo(db, **demo_passwords())

    assert db.scalar(
        select(func.count()).select_from(User).where(User.username.in_(DEMO_USERNAMES))
    ) == 0


@pytest.mark.parametrize(
    ("scope_kind", "is_active"),
    (
        (ScopeKind.GLOBAL.value, True),
        (ScopeKind.UNIT_SUBTREE.value, False),
    ),
)
def test_seed_fails_closed_for_conflicting_or_inactive_demo_review_grant(
    seed_session: tuple[Session, User, Document],
    scope_kind: str,
    is_active: bool,
) -> None:
    db, _, _ = seed_session
    seed = load_seed_module()
    passwords = demo_passwords()
    seed.seed_hr_demo(db, **passwords)
    helen = db.scalar(select(User).where(User.username == "helen.hr.demo"))
    assert helen is not None
    product = db.scalar(
        select(OrganizationUnit).where(
            OrganizationUnit.code == "EAIW-DEMO-PRODUCT"
        )
    )
    assert product is not None
    expected = db.scalar(
        select(CapabilityGrant).where(
            CapabilityGrant.user_id == helen.id,
            CapabilityGrant.capability == Capability.HR_LEAVE_REVIEW.value,
            CapabilityGrant.scope_kind == ScopeKind.UNIT_SUBTREE.value,
            CapabilityGrant.organization_unit_id == product.id,
            CapabilityGrant.is_active.is_(True),
        )
    )
    assert expected is not None
    expected.is_active = False
    db.flush()
    conflicting = CapabilityGrant(
        user_id=helen.id,
        capability=Capability.HR_LEAVE_REVIEW.value,
        scope_kind=scope_kind,
        organization_unit_id=(
            product.id if scope_kind == ScopeKind.UNIT_SUBTREE.value else None
        ),
        is_active=is_active,
    )
    if scope_kind == ScopeKind.GLOBAL.value:
        db.add(conflicting)
        db.flush()
    else:
        conflicting = expected

    with pytest.raises(SystemExit, match="Demo capability grant"):
        with db.begin_nested():
            seed.seed_hr_demo(db, **passwords)

    db.refresh(conflicting)
    assert conflicting.scope_kind == scope_kind
    assert conflicting.is_active is is_active


def test_seed_fails_closed_for_conflicting_procurement_review_grant(
    seed_session: tuple[Session, User, Document],
) -> None:
    db, _, _ = seed_session
    seed = load_seed_module()
    passwords = demo_passwords()
    seed.seed_hr_demo(db, **passwords)
    manager = db.scalar(
        select(User).where(User.username == "manager.procurement.demo")
    )
    assert manager is not None
    expected = db.scalar(
        select(CapabilityGrant).where(
            CapabilityGrant.user_id == manager.id,
            CapabilityGrant.capability
            == Capability.PROCUREMENT_DEPARTMENT_REVIEW.value,
            CapabilityGrant.is_active.is_(True),
        )
    )
    assert expected is not None
    expected.is_active = False
    db.flush()

    with pytest.raises(SystemExit, match="Demo capability grant"):
        with db.begin_nested():
            seed.seed_hr_demo(db, **passwords)

    db.refresh(expected)
    assert expected.is_active is False


def test_non_test_database_requires_explicit_opt_in() -> None:
    seed = load_seed_module()
    production_url = "postgresql+psycopg://user:pass@localhost/company"
    with pytest.raises(SystemExit, match="allow-non-test-demo-database"):
        seed.validate_database_target(production_url, allow_non_test=False)
    seed.validate_database_target(production_url, allow_non_test=True)
    seed.validate_database_target(
        "postgresql+psycopg://user:pass@localhost/company_test",
        allow_non_test=False,
    )
