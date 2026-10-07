from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from policy_api.database import assert_test_database_url
from policy_api.hr.models import EmployeeProfile
from policy_api.models import User, UserRole
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityResolver,
    OrganizationUnit,
    ScopeKind,
)
from policy_api.workbench.catalog import ModuleCatalog


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
def capability_session() -> tuple[Session, dict[str, object]]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for capability integration tests")
    assert_test_database_url(TEST_DATABASE_URL)
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    command.upgrade(config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    try:
        employee = User(
            username=f"cap-employee-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        hr = User(
            username=f"cap-hr-{suffix}",
            password_hash="hash",
            role=UserRole.HR,
            is_active=True,
        )
        admin = User(
            username=f"cap-admin-{suffix}",
            password_hash="hash",
            role=UserRole.ADMIN,
            is_active=True,
        )
        inactive = User(
            username=f"cap-inactive-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=False,
        )
        session.add_all([employee, hr, admin, inactive])
        session.flush()

        root = OrganizationUnit(code=f"ROOT-{suffix}", name="Root", is_active=True)
        session.add(root)
        session.flush()
        active_child = OrganizationUnit(
            code=f"ACTIVE-{suffix}",
            name="Active child",
            parent_id=root.id,
            is_active=True,
        )
        inactive_child = OrganizationUnit(
            code=f"INACTIVE-{suffix}",
            name="Inactive child",
            parent_id=root.id,
            is_active=False,
        )
        session.add_all([active_child, inactive_child])
        session.flush()
        hidden_grandchild = OrganizationUnit(
            code=f"HIDDEN-{suffix}",
            name="Hidden grandchild",
            parent_id=inactive_child.id,
            is_active=True,
        )
        session.add(hidden_grandchild)
        session.add_all(
            [
                EmployeeProfile(
                    user_id=employee.id,
                    employee_number=f"EMP-{suffix}",
                    display_name="Employee",
                    organization_unit_id=active_child.id,
                    hire_date=date(2024, 1, 1),
                    is_active=True,
                ),
                EmployeeProfile(
                    user_id=hr.id,
                    employee_number=f"HR-{suffix}",
                    display_name="HR",
                    organization_unit_id=root.id,
                    hire_date=date(2024, 1, 1),
                    is_active=True,
                ),
            ]
        )
        session.add_all(
            [
                CapabilityGrant(
                    user_id=hr.id,
                    capability=Capability.HR_LEAVE_REVIEW.value,
                    scope_kind=ScopeKind.UNIT_SUBTREE.value,
                    organization_unit_id=root.id,
                    is_active=True,
                ),
                CapabilityGrant(
                    user_id=admin.id,
                    capability=Capability.KNOWLEDGE_MANAGE.value,
                    scope_kind=ScopeKind.GLOBAL.value,
                    is_active=True,
                ),
                CapabilityGrant(
                    user_id=admin.id,
                    capability=Capability.ORGANIZATION_MANAGE.value,
                    scope_kind=ScopeKind.GLOBAL.value,
                    is_active=True,
                ),
                CapabilityGrant(
                    user_id=admin.id,
                    capability=Capability.ANALYTICS_VIEW.value,
                    scope_kind=ScopeKind.GLOBAL.value,
                    is_active=True,
                ),
                CapabilityGrant(
                    user_id=admin.id,
                    capability=Capability.HR_LEAVE_REVIEW.value,
                    scope_kind=ScopeKind.GLOBAL.value,
                    is_active=False,
                ),
            ]
        )
        session.flush()
        yield session, {
            "employee": employee,
            "hr": hr,
            "admin": admin,
            "inactive": inactive,
            "root": root,
            "active_child": active_child,
            "inactive_child": inactive_child,
            "hidden_grandchild": hidden_grandchild,
        }
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def test_resolver_combines_implicit_capabilities_and_explicit_grants(
    capability_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = capability_session
    resolver = CapabilityResolver()
    employee = values["employee"]
    admin = values["admin"]
    inactive = values["inactive"]

    assert isinstance(employee, User)
    assert isinstance(admin, User)
    assert isinstance(inactive, User)
    assert resolver.has(db, employee, Capability.KNOWLEDGE_ASK)
    assert resolver.has(db, employee, Capability.HR_LEAVE_SELF_SERVICE)
    assert resolver.has(
        db,
        employee,
        Capability.PROCUREMENT_REQUEST_SELF_SERVICE,
    )
    assert resolver.has(
        db,
        values["hr"],  # type: ignore[arg-type]
        Capability.PROCUREMENT_REQUEST_SELF_SERVICE,
    )
    assert not resolver.has(db, admin, Capability.HR_LEAVE_SELF_SERVICE)
    assert not resolver.has(
        db,
        admin,
        Capability.PROCUREMENT_REQUEST_SELF_SERVICE,
    )
    assert not resolver.has(db, admin, Capability.HR_LEAVE_REVIEW)
    assert not resolver.has(db, employee, Capability.APPROVAL_INBOX_VIEW)
    assert not resolver.has(
        db,
        values["hr"],  # type: ignore[arg-type]
        Capability.APPROVAL_INBOX_VIEW,
    )
    assert not resolver.has(
        db,
        values["hr"],  # type: ignore[arg-type]
        Capability.PROCUREMENT_DEPARTMENT_REVIEW,
    )
    assert not resolver.has(
        db,
        admin,
        Capability.PROCUREMENT_FINAL_REVIEW,
    )
    assert resolver.has(db, admin, Capability.ORGANIZATION_MANAGE)
    assert not resolver.has(db, inactive, Capability.KNOWLEDGE_ASK)


def test_procurement_review_and_inbox_capabilities_require_independent_grants(
    capability_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = capability_session
    resolver = CapabilityResolver()
    hr = values["hr"]
    root = values["root"]
    active_child = values["active_child"]
    assert isinstance(hr, User)
    assert isinstance(root, OrganizationUnit)
    assert isinstance(active_child, OrganizationUnit)

    db.add(
        CapabilityGrant(
            user_id=hr.id,
            capability=Capability.PROCUREMENT_FINAL_REVIEW.value,
            scope_kind=ScopeKind.UNIT_SUBTREE.value,
            organization_unit_id=root.id,
            is_active=True,
        )
    )
    db.flush()

    review_scope = resolver.scope_for(
        db,
        hr,
        Capability.PROCUREMENT_FINAL_REVIEW,
    )
    assert review_scope is not None
    assert review_scope.is_global is False
    assert review_scope.organization_unit_ids == frozenset(
        {root.id, active_child.id}
    )
    assert not resolver.has(db, hr, Capability.APPROVAL_INBOX_VIEW)
    assert not resolver.has(
        db,
        hr,
        Capability.PROCUREMENT_DEPARTMENT_REVIEW,
    )

    db.add(
        CapabilityGrant(
            user_id=hr.id,
            capability=Capability.PROCUREMENT_DEPARTMENT_REVIEW.value,
            scope_kind=ScopeKind.GLOBAL.value,
            organization_unit_id=None,
            is_active=True,
        )
    )
    db.flush()

    department_scope = resolver.scope_for(
        db,
        hr,
        Capability.PROCUREMENT_DEPARTMENT_REVIEW,
    )
    assert department_scope is not None
    assert department_scope.is_global is True
    assert not resolver.has(db, hr, Capability.APPROVAL_INBOX_VIEW)

    db.add(
        CapabilityGrant(
            user_id=hr.id,
            capability=Capability.APPROVAL_INBOX_VIEW.value,
            scope_kind=ScopeKind.GLOBAL.value,
            is_active=True,
        )
    )
    db.flush()

    inbox_scope = resolver.scope_for(db, hr, Capability.APPROVAL_INBOX_VIEW)
    assert inbox_scope is not None
    assert inbox_scope.is_global is True
    assert resolver.has(db, hr, Capability.PROCUREMENT_DEPARTMENT_REVIEW)


def test_procurement_self_service_grant_requires_active_employee_profile(
    capability_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = capability_session
    resolver = CapabilityResolver()
    admin = values["admin"]
    assert isinstance(admin, User)
    db.add(
        CapabilityGrant(
            user_id=admin.id,
            capability=Capability.PROCUREMENT_REQUEST_SELF_SERVICE.value,
            scope_kind=ScopeKind.GLOBAL.value,
            organization_unit_id=None,
            is_active=True,
        )
    )
    db.flush()

    assert resolver.scope_for(
        db,
        admin,
        Capability.PROCUREMENT_REQUEST_SELF_SERVICE,
    ) is None


@pytest.mark.parametrize(
    ("scope_kind", "use_root"),
    [
        (ScopeKind.GLOBAL, False),
        (ScopeKind.UNIT_SUBTREE, True),
    ],
)
def test_admin_with_active_profile_can_use_explicit_procurement_grant(
    capability_session: tuple[Session, dict[str, object]],
    scope_kind: ScopeKind,
    use_root: bool,
) -> None:
    db, values = capability_session
    resolver = CapabilityResolver()
    admin = values["admin"]
    root = values["root"]
    active_child = values["active_child"]
    assert isinstance(admin, User)
    assert isinstance(root, OrganizationUnit)
    assert isinstance(active_child, OrganizationUnit)
    db.add(
        EmployeeProfile(
            user_id=admin.id,
            employee_number=f"ADMIN-{uuid.uuid4().hex}",
            display_name="Admin employee",
            organization_unit_id=root.id,
            hire_date=date(2024, 1, 1),
            is_active=True,
        )
    )
    db.add(
        CapabilityGrant(
            user_id=admin.id,
            capability=Capability.PROCUREMENT_REQUEST_SELF_SERVICE.value,
            scope_kind=scope_kind.value,
            organization_unit_id=root.id if use_root else None,
            is_active=True,
        )
    )
    db.flush()

    scope = resolver.scope_for(
        db,
        admin,
        Capability.PROCUREMENT_REQUEST_SELF_SERVICE,
    )

    assert scope is not None
    assert scope.is_global is (scope_kind == ScopeKind.GLOBAL)
    assert scope.organization_unit_ids == (
        frozenset()
        if scope_kind == ScopeKind.GLOBAL
        else frozenset({root.id, active_child.id})
    )


def test_subtree_scope_contains_only_active_reachable_units(
    capability_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = capability_session
    resolver = CapabilityResolver()
    hr = values["hr"]
    assert isinstance(hr, User)

    scope = resolver.scope_for(db, hr, Capability.HR_LEAVE_REVIEW)

    assert scope is not None
    assert scope.is_global is False
    assert scope.organization_unit_ids == frozenset(
        {values["root"].id, values["active_child"].id}  # type: ignore[union-attr]
    )
    assert values["inactive_child"].id not in scope.organization_unit_ids  # type: ignore[union-attr]
    assert values["hidden_grandchild"].id not in scope.organization_unit_ids  # type: ignore[union-attr]


def test_module_catalog_filters_in_server_order_from_resolved_capabilities(
    capability_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = capability_session
    catalog = ModuleCatalog(CapabilityResolver())

    employee_modules = catalog.allowed_modules(db, values["employee"])  # type: ignore[arg-type]
    hr_modules = catalog.allowed_modules(db, values["hr"])  # type: ignore[arg-type]
    admin_modules = catalog.allowed_modules(db, values["admin"])  # type: ignore[arg-type]

    assert [item.key for item in employee_modules] == [
        "knowledge",
        "hr-assistant",
        "my-requests",
        "procurement",
    ]
    assert [item.key for item in hr_modules] == [
        "knowledge",
        "hr-assistant",
        "my-requests",
        "hr-review",
        "procurement",
    ]
    assert [item.key for item in admin_modules] == [
        "knowledge",
        "knowledge-admin",
        "organization",
        "analytics",
    ]
