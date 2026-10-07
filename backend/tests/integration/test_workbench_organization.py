from __future__ import annotations

import os
from pathlib import Path
import uuid
from datetime import date

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from policy_api.database import assert_test_database_url
from policy_api.hr.models import EmployeeProfile
from policy_api.models import User, UserRole
from policy_api.workbench.audit import (
    SecurityAuditEvent,
    SecurityAuditValidationError,
    append_security_audit,
)
from policy_api.workbench.capabilities import (
    OrganizationManagementError,
    OrganizationService,
    OrganizationUnit,
)


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
def organization_session() -> tuple[Session, User]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for organization integration tests")
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
    session = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        actor = User(
            username=f"audit-admin-{uuid.uuid4().hex}",
            password_hash="hash",
            role=UserRole.ADMIN,
            is_active=True,
        )
        session.add(actor)
        session.flush()
        yield session, actor
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def grant_audit_arguments(actor: User) -> dict[str, object]:
    grant_id = uuid.uuid4()
    return {
        "event_name": "capability_grant_created",
        "actor_user_id": actor.id,
        "target_type": "capability_grant",
        "target_id": grant_id,
        "operation_id": uuid.uuid4(),
        "outcome": "succeeded",
        "request_id": "trace-audit-1",
        "summary": {
            "grant_id": grant_id,
            "capability": "organization.manage",
            "scope_kind": "global",
            "organization_unit_id": None,
        },
    }


def test_security_audit_replays_canonical_payload_and_rejects_conflict(
    organization_session: tuple[Session, User],
) -> None:
    db, actor = organization_session
    arguments = grant_audit_arguments(actor)

    first = append_security_audit(db, **arguments)
    replay_arguments = dict(arguments)
    summary = arguments["summary"]
    assert isinstance(summary, dict)
    replay_arguments["summary"] = {
        **summary,
        "grant_id": str(arguments["target_id"]),
    }
    replay = append_security_audit(db, **replay_arguments)

    assert replay.id == first.id
    assert db.scalar(select(func.count()).select_from(SecurityAuditEvent)) == 1

    conflicting = dict(arguments)
    conflicting["summary"] = {
        **summary,
        "capability": "analytics.view",
    }
    with pytest.raises(
        SecurityAuditValidationError,
        match="security_audit_operation_conflict",
    ):
        append_security_audit(db, **conflicting)


def test_audit_validation_failure_rolls_back_management_savepoint(
    organization_session: tuple[Session, User],
) -> None:
    db, actor = organization_session
    code = f"ROLLBACK-{uuid.uuid4().hex}"

    with pytest.raises(SecurityAuditValidationError):
        with db.begin_nested():
            unit = OrganizationUnit(code=code, name="Must roll back", is_active=True)
            db.add(unit)
            db.flush()
            append_security_audit(
                db,
                event_name="organization_unit_created",
                actor_user_id=actor.id,
                target_type="organization_unit",
                target_id=unit.id,
                operation_id=uuid.uuid4(),
                outcome="succeeded",
                request_id="trace-rollback",
                summary={"reason": "free text must fail"},
            )

    assert db.scalar(
        select(OrganizationUnit.id).where(OrganizationUnit.code == code)
    ) is None


def test_organization_service_replay_keeps_one_audit_and_audit_failure_rolls_back(
    organization_session: tuple[Session, User],
) -> None:
    db, actor = organization_session
    service = OrganizationService()
    operation_id = uuid.uuid4()
    code = f"SERVICE-{uuid.uuid4().hex.upper()}"

    created = service.create_unit(
        db,
        actor=actor,
        operation_id=operation_id,
        code=code,
        name="Service Unit",
        parent_id=None,
        request_id="trace-service-create",
    )
    replay = service.create_unit(
        db,
        actor=actor,
        operation_id=operation_id,
        code=code,
        name="Service Unit",
        parent_id=None,
        request_id="trace-service-create-replay",
    )

    assert replay.id == created.id
    assert db.scalar(
        select(func.count()).select_from(SecurityAuditEvent).where(
            SecurityAuditEvent.actor_user_id == actor.id,
            SecurityAuditEvent.operation_id == operation_id,
        )
    ) == 1

    rejected_code = f"ROLLBACK-{uuid.uuid4().hex.upper()}"
    with pytest.raises(
        OrganizationManagementError,
        match="security_audit_request_id_invalid",
    ):
        service.create_unit(
            db,
            actor=actor,
            operation_id=uuid.uuid4(),
            code=rejected_code,
            name="Must Roll Back",
            parent_id=None,
            request_id="invalid\nrequest-id",
        )

    assert db.scalar(
        select(OrganizationUnit.id).where(OrganizationUnit.code == rejected_code)
    ) is None


def test_manager_must_own_the_employee_active_organization_subtree(
    organization_session: tuple[Session, User],
) -> None:
    db, actor = organization_session
    suffix = uuid.uuid4().hex
    manager_unit = OrganizationUnit(
        code=f"MANAGER-{suffix.upper()}",
        name="Manager Unit",
        is_active=True,
    )
    employee_unit = OrganizationUnit(
        code=f"EMPLOYEE-{suffix.upper()}",
        name="Employee Unit",
        is_active=True,
    )
    manager_user = User(
        username=f"manager-{suffix}",
        password_hash="hash",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )
    employee_user = User(
        username=f"employee-{suffix}",
        password_hash="hash",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )
    db.add_all([manager_unit, employee_unit, manager_user, employee_user])
    db.flush()
    manager = EmployeeProfile(
        user_id=manager_user.id,
        employee_number=f"M-{suffix}",
        display_name="Manager",
        organization_unit_id=manager_unit.id,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    employee = EmployeeProfile(
        user_id=employee_user.id,
        employee_number=f"E-{suffix}",
        display_name="Employee",
        organization_unit_id=employee_unit.id,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    db.add_all([manager, employee])
    db.commit()
    employee_id = employee.id
    manager_id = manager.id
    employee_unit_id = employee_unit.id

    with pytest.raises(
        OrganizationManagementError,
        match="manager_assignment_invalid",
    ):
        OrganizationService().update_employee_assignment(
            db,
            actor=actor,
            employee_id=employee_id,
            operation_id=uuid.uuid4(),
            organization_unit_id=employee_unit_id,
            manager_employee_id=manager_id,
            request_id="trace-manager-scope",
        )

    persisted = db.get(EmployeeProfile, employee_id)
    assert persisted is not None
    assert persisted.manager_employee_id is None
