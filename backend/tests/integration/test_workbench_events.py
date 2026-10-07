from __future__ import annotations

import importlib.util
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
from policy_api.workbench.capabilities import (
    CapabilityResolver,
    OrganizationUnit,
)
from policy_api.workbench.audit import SecurityAuditEvent
from policy_api.workbench.catalog import ModuleCatalog
from policy_api.workbench.events import (
    EventInput,
    ProductEvent,
    ProductEventEmitter,
    ProductEventValidationError,
)
from policy_api.workbench.runtime import WorkbenchRuntime


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


def _load_foundation_evaluator():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "evaluate_workbench_foundation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "evaluate_workbench_foundation_event_integration",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("evaluator_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def event_session() -> tuple[Session, User, OrganizationUnit]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for product event integration tests")
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
        suffix = uuid.uuid4().hex
        actor = User(
            username=f"event-employee-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        unit = OrganizationUnit(
            code=f"EVENT-{suffix.upper()}",
            name="Event Unit",
            is_active=True,
        )
        session.add_all([actor, unit])
        session.flush()
        session.add(
            EmployeeProfile(
                user_id=actor.id,
                employee_number=f"EV-{suffix}",
                display_name="Event Employee",
                hire_date=date(2024, 1, 1),
                organization_unit_id=unit.id,
                is_active=True,
            )
        )
        session.flush()
        yield session, actor, unit
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def leave_event(actor: User, unit: OrganizationUnit, **changes: object) -> EventInput:
    values: dict[str, object] = {
        "event_id": uuid.uuid4(),
        "event_name": "leave_request_submitted",
        "module_key": "hr-assistant",
        "actor_user_id": actor.id,
        "organization_unit_id": unit.id,
        "role_snapshot": actor.role.value,
        "request_id": "trace/non-uuid/1",
        "outcome": "succeeded",
        "duration_ms": 12,
        "dimensions": {
            "leave_type": "annual",
            "workday_count_bucket": "1_2",
        },
    }
    values.update(changes)
    return EventInput(**values)  # type: ignore[arg-type]


def test_emitter_replays_canonical_payload_and_rejects_conflict(
    event_session: tuple[Session, User, OrganizationUnit],
) -> None:
    db, actor, unit = event_session
    emitter = ProductEventEmitter()
    event = leave_event(actor, unit)

    first = emitter.append(db, event)
    replay = emitter.append(db, event)

    assert replay.id == first.id
    assert first.request_id == "trace/non-uuid/1"
    assert db.scalar(select(func.count()).select_from(ProductEvent).where(
        ProductEvent.event_id == event.event_id)) == 1

    with pytest.raises(ProductEventValidationError, match="event_id_conflict"):
        emitter.append(db, leave_event(actor, unit, event_id=event.event_id, outcome="failed"))


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"dimensions": {"reason": "不得入库"}}, "event_dimensions_invalid"),
        ({"event_name": "unregistered_event"}, "event_name_not_allowed"),
        ({"request_id": "x" * 121}, "event_request_id_invalid"),
        ({"request_id": "trace\ninvalid"}, "event_request_id_invalid"),
        ({"duration_ms": -1}, "event_duration_invalid"),
        ({"outcome": "employee reason must not persist"}, "event_outcome_invalid"),
        ({"role_snapshot": "executive"}, "event_role_invalid"),
    ],
)
def test_emitter_rejects_unregistered_sensitive_or_invalid_payloads(
    event_session: tuple[Session, User, OrganizationUnit],
    changes: dict[str, object],
    code: str,
) -> None:
    db, actor, unit = event_session
    with pytest.raises(ProductEventValidationError, match=code):
        ProductEventEmitter().append(db, leave_event(actor, unit, **changes))


def test_runtime_injects_actor_organization_snapshot_for_ui_event(
    event_session: tuple[Session, User, OrganizationUnit],
) -> None:
    db, actor, unit = event_session
    resolver = CapabilityResolver()
    runtime = WorkbenchRuntime(
        module_catalog=ModuleCatalog(resolver),
        capability_resolver=resolver,
    )

    event = runtime.record_ui_event(
        db,
        actor=actor,
        event_id=uuid.uuid4(),
        event_name="workbench_module_opened",
        dimensions={"entry_source": "navigation", "module_key": "knowledge"},
        request_id="ui-trace-1",
    )

    assert event.actor_user_id == actor.id
    assert event.organization_unit_id == unit.id
    assert event.role_snapshot == "employee"
    assert event.module_key == "knowledge"


def test_hr_tool_events_accept_the_registered_policy_search_tool(
    event_session: tuple[Session, User, OrganizationUnit],
) -> None:
    db, actor, unit = event_session
    event = ProductEventEmitter().append(
        db,
        EventInput(
            event_id=uuid.uuid4(),
            event_name="tool_read_succeeded",
            module_key="hr-assistant",
            actor_user_id=actor.id,
            organization_unit_id=unit.id,
            role_snapshot=actor.role.value,
            request_id="knowledge-tool-trace",
            outcome="succeeded",
            duration_ms=12,
            dimensions={
                "tool_name": "knowledge.search_policy",
                "processing_time_bucket": "lt_1s",
            },
        ),
    )

    assert event.dimensions["tool_name"] == "knowledge.search_policy"


@pytest.mark.parametrize(
    ("tool_name", "intent", "risk_level"),
    (
        ("hr.get_my_leave_balances", "get_my_leave_balances", "sensitive_read"),
        ("hr.calculate_leave_duration", "calculate_leave_duration", "read"),
        ("hr.list_my_leave_requests", "list_my_leave_requests", "sensitive_read"),
        ("hr.get_my_leave_request", "get_my_leave_request", "sensitive_read"),
        ("hr.submit_leave_request", "submit_leave_request", "write"),
        ("hr.cancel_leave_request", "cancel_leave_request", "write"),
    ),
)
def test_hr_product_events_accept_every_registered_hr_tool_and_intent(
    event_session: tuple[Session, User, OrganizationUnit],
    tool_name: str,
    intent: str,
    risk_level: str,
) -> None:
    db, actor, unit = event_session
    emitter = ProductEventEmitter()

    planned = emitter.append(
        db,
        EventInput(
            event_id=uuid.uuid4(),
            event_name="tool_planned",
            module_key="hr-assistant",
            actor_user_id=actor.id,
            organization_unit_id=unit.id,
            role_snapshot=actor.role.value,
            request_id="registered-tool-trace",
            outcome="planned",
            duration_ms=None,
            dimensions={"tool_name": tool_name, "risk_level": risk_level},
        ),
    )
    resolved = emitter.append(
        db,
        EventInput(
            event_id=uuid.uuid4(),
            event_name="hr_intent_resolved",
            module_key="hr-assistant",
            actor_user_id=actor.id,
            organization_unit_id=unit.id,
            role_snapshot=actor.role.value,
            request_id="registered-intent-trace",
            outcome="resolved",
            duration_ms=None,
            dimensions={
                "intent": intent,
                "clarification_required": False,
            },
        ),
    )

    assert planned.dimensions["tool_name"] == tool_name
    assert planned.dimensions["risk_level"] == risk_level
    assert resolved.dimensions["intent"] == intent


def test_foundation_evaluator_detects_database_level_sensitive_sentinels(
    event_session: tuple[Session, User, OrganizationUnit],
) -> None:
    db, actor, unit = event_session
    db.add_all(
        [
            ProductEvent(
                event_id=uuid.uuid4(),
                event_name="question_submitted",
                module_key="knowledge",
                actor_user_id=actor.id,
                organization_unit_id=unit.id,
                role_snapshot=actor.role.value,
                request_id="privacy-product-event",
                outcome="secret-marker",
                duration_ms=None,
                dimensions={"message_length_bucket": "0_50"},
            ),
            SecurityAuditEvent(
                event_name="organization_unit_created",
                actor_user_id=actor.id,
                target_type="organization_unit",
                target_id=unit.id,
                operation_id=uuid.uuid4(),
                outcome="succeeded",
                request_id="cookie-marker",
                summary={
                    "organization_unit_id": str(unit.id),
                    "parent_id": None,
                    "changed_fields": ["code"],
                },
            ),
        ]
    )
    db.flush()

    evaluator = _load_foundation_evaluator()
    observations = evaluator._event_privacy_observations(
        db,
        ("secret-marker", "cookie-marker"),
    )

    assert observations == (1, 1, 1, 1, 0, 1)
