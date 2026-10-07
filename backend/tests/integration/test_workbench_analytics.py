from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
import math
import os
from pathlib import Path
import time
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, event as sqlalchemy_event, func, insert, select, text, update
from sqlalchemy.orm import Session

from policy_api.hr.enums import LeaveRequestStatus, LeaveTypeCode
from policy_api.hr.models import EmployeeProfile, LeaveRequest, LeaveType
from policy_api.models import User, UserRole
from policy_api.workbench.audit import SecurityAuditEvent
from policy_api.workbench.analytics import (
    AnalyticsError,
    AnalyticsService,
    resolve_window,
)
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    OrganizationUnit,
    ScopeKind,
)
from policy_api.workbench.events import ProductEvent


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _load_foundation_evaluator():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "evaluate_workbench_foundation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "evaluate_workbench_foundation_integration",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("evaluator_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def analytics_session() -> tuple[Session, dict[str, object]]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for analytics integration tests")
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    command.upgrade(config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    suffix = uuid.uuid4().hex
    try:
        db.execute(update(User).values(is_active=False))
        global_viewer = User(
            username=f"analytics-global-{suffix}",
            password_hash="hash",
            role=UserRole.ADMIN,
            is_active=True,
        )
        scoped_viewer = User(
            username=f"analytics-scoped-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        unauthorized = User(
            username=f"analytics-none-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        unit_a = OrganizationUnit(
            code=f"ANALYTICS-A-{suffix.upper()}", name="Analytics A", is_active=True
        )
        unit_b = OrganizationUnit(
            code=f"ANALYTICS-B-{suffix.upper()}", name="Analytics B", is_active=True
        )
        db.add_all([global_viewer, scoped_viewer, unauthorized, unit_a, unit_b])
        db.flush()
        db.add_all(
            [
                CapabilityGrant(
                    user_id=global_viewer.id,
                    capability=Capability.ANALYTICS_VIEW.value,
                    scope_kind=ScopeKind.GLOBAL.value,
                    is_active=True,
                ),
                CapabilityGrant(
                    user_id=scoped_viewer.id,
                    capability=Capability.ANALYTICS_VIEW.value,
                    scope_kind=ScopeKind.UNIT_SUBTREE.value,
                    organization_unit_id=unit_a.id,
                    is_active=True,
                ),
            ]
        )
        _seed_knowledge_events(db, global_viewer.id, unit_a.id, count=10)
        _seed_knowledge_events(db, global_viewer.id, unit_b.id, count=2)
        db.commit()
        yield db, {
            "global": global_viewer,
            "scoped": scoped_viewer,
            "unauthorized": unauthorized,
            "unit_a": unit_a,
            "unit_b": unit_b,
        }
    finally:
        db.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as maintenance:
            maintenance.exec_driver_sql("VACUUM (ANALYZE) product_events")
        engine.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _event(
    *,
    actor_id: uuid.UUID,
    organization_unit_id: uuid.UUID,
    event_name: str,
    duration_ms: int | None,
    dimensions: dict[str, object],
    module_key: str = "knowledge",
) -> ProductEvent:
    return ProductEvent(
        event_id=uuid.uuid4(),
        event_name=event_name,
        module_key=module_key,
        actor_user_id=actor_id,
        organization_unit_id=organization_unit_id,
        role_snapshot="employee",
        request_id=f"analytics-{uuid.uuid4()}",
        outcome="succeeded",
        duration_ms=duration_ms,
        dimensions=dimensions,
        occurred_at=NOW - timedelta(days=1),
    )


def _seed_knowledge_events(
    db: Session,
    actor_id: uuid.UUID,
    organization_unit_id: uuid.UUID,
    *,
    count: int,
) -> None:
    for index in range(count):
        db.add(
            _event(
                actor_id=actor_id,
                organization_unit_id=organization_unit_id,
                event_name="question_submitted",
                duration_ms=None,
                dimensions={"message_length_bucket": "0_50"},
            )
        )
        if count >= 10 and index < 5:
            terminal_name = "question_answered"
            dimensions = {"has_citations": True, "processing_time_bucket": "lt_1s"}
        elif count >= 10 and index < 8:
            terminal_name = "question_clarification_requested"
            dimensions = {
                "question_count_bucket": "1_3",
                "has_citations": True,
                "processing_time_bucket": "lt_1s",
            }
        else:
            terminal_name = "question_abstained"
            dimensions = {
                "error_code": (
                    "model_provider_error" if index == count - 1 else "insufficient_evidence"
                )
            }
        db.add(
            _event(
                actor_id=actor_id,
                organization_unit_id=organization_unit_id,
                event_name=terminal_name,
                duration_ms=(index + 1) * 100,
                dimensions=dimensions,
            )
        )
    for helpful in (True, False):
        db.add(
            _event(
                actor_id=actor_id,
                organization_unit_id=organization_unit_id,
                event_name="answer_feedback_submitted",
                duration_ms=None,
                dimensions={"helpful": helpful},
            )
        )


def test_knowledge_metrics_use_snapshot_scope_percentiles_and_small_segment_hiding(
    analytics_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = analytics_session
    service = AnalyticsService()
    window = resolve_window(NOW - timedelta(days=7), NOW, now=NOW)

    scoped = service.knowledge(db, values["scoped"], window, None)  # type: ignore[arg-type]
    assert scoped.organization_unit_id is None
    assert scoped.metrics["questions_submitted"].value == 10
    assert scoped.metrics["evidence_answer_rate"].numerator == 5
    assert scoped.metrics["evidence_answer_rate"].denominator == 10
    assert scoped.metrics["clarification_rate"].numerator == 3
    assert scoped.metrics["clarification_rate"].denominator == 10
    assert scoped.metrics["clarification_rate"].value == pytest.approx(3 / 10)
    assert scoped.metrics["abstention_rate"].numerator == 2
    assert scoped.metrics["abstention_rate"].denominator == 10
    assert scoped.metrics["abstention_rate"].value == pytest.approx(2 / 10)
    assert scoped.metrics["helpful_feedback_rate"].value == 0.5
    assert scoped.metrics["model_failure_rate"].value == pytest.approx(1 / 10)
    assert scoped.metrics["response_latency_p50_ms"].value == 550
    assert scoped.metrics["response_latency_p95_ms"].value == pytest.approx(955)
    assert scoped.metrics["response_latency_p95_ms"].sample_size == 10

    hidden = service.knowledge(
        db,
        values["global"],  # type: ignore[arg-type]
        window,
        values["unit_b"].id,  # type: ignore[union-attr]
    )
    assert hidden.organization_unit_id == values["unit_b"].id  # type: ignore[union-attr]
    assert hidden.metrics["questions_submitted"].available is False
    assert hidden.metrics["questions_submitted"].value is None
    assert hidden.metrics["questions_submitted"].sample_size == 2


def test_analytics_scope_rejects_missing_capability_and_foreign_unit(
    analytics_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = analytics_session
    service = AnalyticsService()
    window = resolve_window(NOW - timedelta(days=7), NOW, now=NOW)

    with pytest.raises(AnalyticsError, match="analytics_scope_forbidden"):
        service.knowledge(
            db, values["unauthorized"], window, None  # type: ignore[arg-type]
        )
    with pytest.raises(AnalyticsError, match="analytics_scope_forbidden"):
        service.knowledge(
            db,
            values["scoped"],  # type: ignore[arg-type]
            window,
            values["unit_b"].id,  # type: ignore[union-attr]
        )


def test_foundation_evaluator_probes_denied_and_foreign_hr_requests(
    analytics_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = analytics_session
    suffix = uuid.uuid4().hex
    db.add(
        CapabilityGrant(
            user_id=values["scoped"].id,  # type: ignore[union-attr]
            capability=Capability.HR_LEAVE_REVIEW.value,
            scope_kind=ScopeKind.UNIT_SUBTREE.value,
            organization_unit_id=values["unit_a"].id,  # type: ignore[union-attr]
            is_active=True,
        )
    )
    leave_type = LeaveType(
        code=LeaveTypeCode.ANNUAL,
        display_name=f"Evaluator Annual {suffix}",
        is_enabled=True,
    )
    employee = EmployeeProfile(
        user_id=values["unauthorized"].id,  # type: ignore[union-attr]
        employee_number=f"EVAL-{suffix}",
        display_name="Evaluator Foreign Employee",
        organization_unit_id=values["unit_b"].id,  # type: ignore[union-attr]
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    db.add_all([leave_type, employee])
    db.flush()
    db.add(
        LeaveRequest(
            request_number=f"EVAL-{suffix}",
            employee_id=employee.id,
            leave_type_id=leave_type.id,
            start_date=date(2026, 8, 20),
            end_date=date(2026, 8, 20),
            workday_count=Decimal("1"),
            reason="evaluator foreign request fixture",
            status=LeaveRequestStatus.PENDING,
            submitted_at=NOW - timedelta(days=1),
        )
    )
    db.flush()

    evaluator = _load_foundation_evaluator()
    forbidden_successes, cases = evaluator._scoped_hr_observations(db)

    assert forbidden_successes == 0
    assert cases == 3


def test_overview_combines_fixed_event_counts_with_current_pending(
    analytics_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = analytics_session
    db.add_all(
        [
            _event(
                actor_id=values["global"].id,  # type: ignore[union-attr]
                organization_unit_id=values["unit_a"].id,  # type: ignore[union-attr]
                event_name="leave_request_submitted",
                duration_ms=None,
                dimensions={
                    "leave_type": "annual",
                    "workday_count_bucket": "1_2",
                },
            ),
            _event(
                actor_id=values["global"].id,  # type: ignore[union-attr]
                organization_unit_id=values["unit_a"].id,  # type: ignore[union-attr]
                event_name="tool_planned",
                duration_ms=None,
                dimensions={
                    "tool_name": "hr.submit_leave_request",
                    "risk_level": "write",
                },
            ),
        ]
    )
    db.flush()
    service = AnalyticsService()
    window = resolve_window(NOW - timedelta(days=7), NOW, now=NOW)

    result = service.overview(
        db, values["scoped"], window, None  # type: ignore[arg-type]
    )

    assert result.metrics["questions_submitted"].value == 10
    assert result.metrics["leave_requests_submitted"].value == 1
    assert result.metrics["tools_planned"].value == 1
    assert result.metrics["leave_requests_pending"].value == 0


def test_hr_funnel_reports_fixed_stage_counts_and_adjacent_conversion_rates(
    analytics_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = analytics_session
    stages = [
        ("hr_turn_submitted", 6, {"message_length_bucket": "0_50"}),
        (
            "hr_intent_resolved",
            5,
            {"intent": "submit_leave_request", "clarification_required": False},
        ),
        (
            "tool_planned",
            4,
            {"tool_name": "hr.submit_leave_request", "risk_level": "write"},
        ),
        (
            "confirmation_shown",
            4,
            {"tool_name": "hr.submit_leave_request", "risk_level": "write"},
        ),
        (
            "confirmation_confirmed",
            3,
            {"tool_name": "hr.submit_leave_request"},
        ),
        (
            "leave_request_submitted",
            3,
            {"leave_type": "annual", "workday_count_bucket": "1_2"},
        ),
        (
            "leave_request_reviewed",
            2,
            {"decision": "approved", "processing_time_bucket": "same_day"},
        ),
    ]
    for event_name, count, dimensions in stages:
        for _ in range(count):
            db.add(
                _event(
                    actor_id=values["global"].id,  # type: ignore[union-attr]
                    organization_unit_id=values["unit_a"].id,  # type: ignore[union-attr]
                    event_name=event_name,
                    duration_ms=None,
                    dimensions=dimensions,
                )
            )
    db.flush()
    service = AnalyticsService()
    window = resolve_window(NOW - timedelta(days=7), NOW, now=NOW)

    result = service.hr_funnel(
        db, values["scoped"], window, None  # type: ignore[arg-type]
    )

    assert result.metrics["hr_turns_submitted"].value == 6
    assert result.metrics["intents_resolved"].value == 5
    assert result.metrics["write_tools_planned"].value == 4
    assert result.metrics["confirmations_shown"].value == 4
    assert result.metrics["confirmations_confirmed"].value == 3
    assert result.metrics["leave_requests_submitted"].value == 3
    assert result.metrics["leave_requests_reviewed"].value == 2
    assert result.metrics["intent_resolution_rate"].value == pytest.approx(5 / 6)
    assert result.metrics["write_tool_plan_rate"].value == pytest.approx(4 / 5)
    assert result.metrics["confirmation_show_rate"].value == 1
    assert result.metrics["confirmation_confirm_rate"].value == pytest.approx(3 / 4)
    assert result.metrics["leave_submission_rate"].value == 1
    assert result.metrics["leave_review_rate"].value == pytest.approx(2 / 3)


def test_tool_metrics_report_fixed_error_denominators_and_read_percentiles(
    analytics_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = analytics_session

    def add_event(
        event_name: str,
        dimensions: dict[str, object],
        *,
        duration_ms: int | None = None,
    ) -> None:
        db.add(
            _event(
                actor_id=values["global"].id,  # type: ignore[union-attr]
                organization_unit_id=values["unit_a"].id,  # type: ignore[union-attr]
                event_name=event_name,
                duration_ms=duration_ms,
                dimensions=dimensions,
            )
        )

    for _ in range(6):
        add_event("hr_turn_submitted", {"message_length_bucket": "0_50"})
    for index in range(6):
        add_event(
            "tool_planned",
            {
                "tool_name": (
                    "hr.get_my_leave_balances"
                    if index < 4
                    else "hr.submit_leave_request"
                ),
                "risk_level": "sensitive_read" if index < 4 else "write",
            },
        )
    add_event(
        "tool_validation_failed",
        {
            "tool_name": "hr.submit_leave_request",
            "error_code": "tool_arguments_invalid",
            "retryable": False,
        },
    )
    add_event(
        "hr_flow_error",
        {"error_code": "model_provider_error", "retryable": True},
    )
    for _ in range(2):
        add_event(
            "confirmation_shown",
            {"tool_name": "hr.submit_leave_request", "risk_level": "write"},
        )
    add_event(
        "confirmation_expired",
        {"tool_name": "hr.submit_leave_request"},
    )
    for duration_ms in (100, 200, 300, 400, 500, 600):
        add_event(
            "tool_read_succeeded",
            {
                "tool_name": "hr.get_my_leave_balances",
                "processing_time_bucket": "lt_1s",
            },
            duration_ms=duration_ms,
        )
    db.flush()
    service = AnalyticsService()
    window = resolve_window(NOW - timedelta(days=7), NOW, now=NOW)

    result = service.tools(
        db, values["scoped"], window, None  # type: ignore[arg-type]
    )

    assert result.metrics["tools_planned"].value == 6
    assert result.metrics["read_tools_planned"].value == 4
    assert result.metrics["write_tools_planned"].value == 2
    assert result.metrics["validation_failure_rate"].value == pytest.approx(1 / 6)
    assert result.metrics["provider_failure_rate"].value == pytest.approx(1 / 6)
    assert result.metrics["confirmation_expiry_rate"].value == 0.5
    assert result.metrics["read_latency_p50_ms"].value == 350
    assert result.metrics["read_latency_p95_ms"].value == 575
    assert result.metrics["read_latency_p95_ms"].sample_size == 6


def test_workflow_metrics_use_current_employee_scope_and_database_percentiles(
    analytics_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = analytics_session
    suffix = uuid.uuid4().hex
    leave_type = LeaveType(
        code=LeaveTypeCode.ANNUAL,
        display_name=f"Annual {suffix}",
        is_enabled=True,
    )
    employee_a = EmployeeProfile(
        user_id=values["global"].id,  # type: ignore[union-attr]
        employee_number=f"AN-A-{suffix}",
        display_name="Analytics A Employee",
        organization_unit_id=values["unit_a"].id,  # type: ignore[union-attr]
        hire_date=date(2020, 1, 1),
        is_active=True,
    )
    employee_b = EmployeeProfile(
        user_id=values["unauthorized"].id,  # type: ignore[union-attr]
        employee_number=f"AN-B-{suffix}",
        display_name="Analytics B Employee",
        organization_unit_id=values["unit_b"].id,  # type: ignore[union-attr]
        hire_date=date(2020, 1, 1),
        is_active=True,
    )
    db.add_all([leave_type, employee_a, employee_b])
    db.flush()

    def request(
        index: int,
        employee: EmployeeProfile,
        status: LeaveRequestStatus,
        submitted_at: datetime,
        *,
        processed_after: timedelta | None = None,
    ) -> LeaveRequest:
        reviewed = status in {
            LeaveRequestStatus.APPROVED,
            LeaveRequestStatus.REJECTED,
        }
        cancelled = status == LeaveRequestStatus.CANCELLED
        processed_at = (
            submitted_at + processed_after
            if processed_after is not None
            else None
        )
        return LeaveRequest(
            request_number=f"AN-{suffix}-{index}",
            employee_id=employee.id,
            leave_type_id=leave_type.id,
            start_date=date(2026, 8, 20),
            end_date=date(2026, 8, 20),
            workday_count=Decimal("1"),
            reason="synthetic analytics fixture",
            status=status,
            submitted_at=submitted_at,
            reviewer_user_id=(values["global"].id if reviewed else None),  # type: ignore[union-attr]
            reviewed_at=(processed_at if reviewed else None),
            rejection_reason=(
                "fixture rejection"
                if status == LeaveRequestStatus.REJECTED
                else None
            ),
            cancelled_at=(processed_at if cancelled else None),
            cancelled_by_user_id=(
                values["global"].id if cancelled else None  # type: ignore[union-attr]
            ),
        )

    db.add_all(
        [
            request(1, employee_a, LeaveRequestStatus.PENDING, NOW - timedelta(days=2)),
            request(2, employee_a, LeaveRequestStatus.PENDING, NOW - timedelta(hours=2)),
            request(
                3,
                employee_a,
                LeaveRequestStatus.APPROVED,
                NOW - timedelta(days=5),
                processed_after=timedelta(hours=1),
            ),
            request(
                4,
                employee_a,
                LeaveRequestStatus.APPROVED,
                NOW - timedelta(days=4),
                processed_after=timedelta(hours=3),
            ),
            request(
                5,
                employee_a,
                LeaveRequestStatus.REJECTED,
                NOW - timedelta(days=3),
                processed_after=timedelta(hours=5),
            ),
            request(
                6,
                employee_a,
                LeaveRequestStatus.CANCELLED,
                NOW - timedelta(days=2),
                processed_after=timedelta(hours=1),
            ),
            request(7, employee_b, LeaveRequestStatus.PENDING, NOW - timedelta(days=2)),
        ]
    )
    db.flush()
    service = AnalyticsService()
    window = resolve_window(NOW - timedelta(days=7), NOW, now=NOW)

    result = service.workflows(
        db, values["scoped"], window, None  # type: ignore[arg-type]
    )

    assert result.metrics["leave_requests_pending"].value == 2
    assert result.metrics["leave_requests_submitted"].value == 6
    assert result.metrics["leave_requests_processed"].value == 4
    assert result.metrics["leave_requests_approved"].value == 2
    assert result.metrics["leave_requests_rejected"].value == 1
    assert result.metrics["leave_requests_cancelled"].value == 1
    assert result.metrics["review_duration_p50_ms"].value == 10_800_000
    assert result.metrics["review_duration_p95_ms"].value == 17_280_000
    assert result.metrics["review_duration_p95_ms"].sample_size == 3
    assert result.metrics["leave_requests_pending_over_24h"].value == 1


def test_procurement_funnel_uses_scoped_events_audits_and_database_percentiles(
    analytics_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = analytics_session
    actor = values["global"]
    unit_a = values["unit_a"]
    unit_b = values["unit_b"]
    assert isinstance(actor, User)
    assert isinstance(unit_a, OrganizationUnit)
    assert isinstance(unit_b, OrganizationUnit)

    def add_event(
        event_name: str,
        dimensions: dict[str, object],
        *,
        duration_ms: int | None = None,
        organization: OrganizationUnit = unit_a,
    ) -> None:
        db.add(
            _event(
                actor_id=actor.id,
                organization_unit_id=organization.id,
                event_name=event_name,
                duration_ms=duration_ms,
                dimensions=dimensions,
                module_key="procurement",
            )
        )

    for index in range(6):
        add_event(
            "procurement_request_submitted",
            {
                "stage": "submission",
                "channel": "ai_confirmation" if index < 3 else "manual",
                "item_count_bucket": "1",
                "amount_bucket": "0_999",
            },
        )
    add_event(
        "procurement_request_withdrawn",
        {"stage": "withdrawal", "processing_time_bucket": "same_day"},
    )
    for duration_ms in (1_000, 2_000, 3_000, 4_000, 5_000, 6_000):
        add_event(
            "approval_task_approved" if duration_ms <= 4_000 else "approval_task_rejected",
            {
                "stage": "department_review",
                "processing_time_bucket": "same_day",
            },
            duration_ms=duration_ms,
        )
    for duration_ms in (2_000, 4_000, 6_000, 8_000):
        add_event(
            "approval_task_approved" if duration_ms <= 6_000 else "approval_task_rejected",
            {
                "stage": "procurement_review",
                "processing_time_bucket": "same_day",
            },
            duration_ms=duration_ms,
        )
    for duration_ms in (10_000, 20_000, 30_000, 40_000):
        add_event(
            "procurement_request_completed",
            {
                "stage": "completion",
                "outcome": "approved",
                "processing_time_bucket": "same_day",
            },
            duration_ms=duration_ms,
        )
    for _ in range(4):
        add_event(
            "confirmation_shown",
            {"tool_name": "procurement.submit_request", "risk_level": "write"},
        )
    for _ in range(3):
        add_event(
            "confirmation_confirmed",
            {"tool_name": "procurement.submit_request"},
        )
    for _ in range(2):
        add_event(
            "tool_validation_failed",
            {
                "tool_name": "procurement.submit_request",
                "error_code": "tool_arguments_invalid",
                "retryable": False,
            },
        )

    request_ids = [uuid.uuid4() for _ in range(5)]

    def add_audit(
        event_name: str,
        request_id: uuid.UUID,
        *,
        organization: OrganizationUnit = unit_a,
        stage: str | None = None,
    ) -> None:
        summary = {
            "procurement_request_id": str(request_id),
            "organization_unit_id": str(organization.id),
        }
        if stage is not None:
            summary["stage"] = stage
        db.add(
            SecurityAuditEvent(
                event_name=event_name,
                actor_user_id=actor.id,
                target_type="procurement_request",
                target_id=request_id,
                operation_id=uuid.uuid4(),
                outcome="succeeded",
                request_id=f"analytics-{uuid.uuid4()}",
                summary=summary,
                occurred_at=NOW - timedelta(days=1),
            )
        )

    for request_id in request_ids[:4]:
        add_audit("procurement_request_submitted", request_id)
    add_audit("approval_task_approved", request_ids[0], stage="department_review")
    add_audit("approval_task_approved", request_ids[1], stage="procurement_review")
    add_audit("approval_task_rejected", request_ids[2], stage="department_review")
    add_audit("procurement_request_withdrawn", request_ids[3], stage="withdrawal")
    add_audit("approval_task_approved", request_ids[4], stage="procurement_review")
    add_audit("procurement_permission_denied", uuid.uuid4())
    add_audit("procurement_operation_conflict", uuid.uuid4())
    add_audit("procurement_operation_replayed", uuid.uuid4())

    db.flush()
    service = AnalyticsService()
    window = resolve_window(NOW - timedelta(days=7), NOW, now=NOW)

    statements: list[str] = []
    connection = db.connection()

    def record_statement(
        _connection, _cursor, statement: str, *_args: object
    ) -> None:
        statements.append(statement)

    sqlalchemy_event.listen(
        connection, "before_cursor_execute", record_statement
    )
    try:
        result = service.procurement_funnel(db, actor, window, None)
    finally:
        sqlalchemy_event.remove(
            connection, "before_cursor_execute", record_statement
        )

    audit_statements = [
        statement
        for statement in statements
        if "security_audit_events" in statement
    ]
    assert len(audit_statements) == 2
    assert all(
        "JOIN security_audit_events" not in statement
        for statement in audit_statements
    )

    assert result.metrics["procurement_requests_submitted"].value == 6
    assert result.metrics["withdrawal_rate"].value == pytest.approx(1 / 6)
    assert result.metrics["department_approval_rate"].value == pytest.approx(4 / 6)
    assert result.metrics["department_rejection_rate"].value == pytest.approx(2 / 6)
    assert result.metrics["procurement_approval_rate"].value == pytest.approx(3 / 4)
    assert result.metrics["procurement_rejection_rate"].value == pytest.approx(1 / 4)
    assert result.metrics["department_processing_p50_ms"].value == 3_500
    assert result.metrics["department_processing_p95_ms"].value == 5_750
    assert result.metrics["procurement_processing_p50_ms"].value == 5_000
    assert result.metrics["procurement_processing_p95_ms"].value == pytest.approx(7_700)
    assert result.metrics["flow_processing_p50_ms"].value == 25_000
    assert result.metrics["flow_processing_p95_ms"].value == 38_500
    assert result.metrics["ai_assisted_submission_rate"].value == pytest.approx(3 / 6)
    assert result.metrics["confirmation_conversion_rate"].value == pytest.approx(3 / 4)
    assert result.metrics["tool_validation_failures"].value == 2
    assert result.metrics["permission_denials"].value == 1
    assert result.metrics["state_conflicts"].value == 1
    assert result.metrics["replays"].value == 1
    assert result.metrics["audited_terminal_requests"].value == 3

    for _ in range(2):
        add_event(
            "procurement_request_submitted",
            {
                "stage": "submission",
                "channel": "manual",
                "item_count_bucket": "1",
                "amount_bucket": "0_999",
            },
            organization=unit_b,
        )
    db.flush()
    hidden = service.procurement_funnel(db, actor, window, unit_b.id)
    assert hidden.organization_unit_id == unit_b.id
    assert all(not metric.available for metric in hidden.metrics.values())


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


def test_fixed_analytics_queries_meet_100k_event_budget(
    analytics_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = analytics_session
    actor = values["global"]
    unit = values["unit_a"]
    event_specs = (
        ("question_submitted", "knowledge", None, {"message_length_bucket": "0_50"}),
        (
            "question_answered",
            "knowledge",
            120,
            {"has_citations": True, "processing_time_bucket": "lt_1s"},
        ),
        ("hr_turn_submitted", "hr-assistant", None, {"message_length_bucket": "0_50"}),
        (
            "hr_intent_resolved",
            "hr-assistant",
            None,
            {"intent": "get_my_leave_balances", "clarification_required": False},
        ),
        (
            "tool_planned",
            "hr-assistant",
            None,
            {"tool_name": "hr.get_my_leave_balances", "risk_level": "sensitive_read"},
        ),
        (
            "tool_read_succeeded",
            "hr-assistant",
            80,
            {"tool_name": "hr.get_my_leave_balances", "processing_time_bucket": "lt_1s"},
        ),
        (
            "confirmation_shown",
            "hr-assistant",
            None,
            {"tool_name": "hr.submit_leave_request", "risk_level": "write"},
        ),
        (
            "confirmation_confirmed",
            "hr-assistant",
            None,
            {"tool_name": "hr.submit_leave_request"},
        ),
        (
            "leave_request_submitted",
            "hr-assistant",
            None,
            {"leave_type": "annual", "workday_count_bucket": "1_2"},
        ),
        (
            "leave_request_reviewed",
            "hr-review",
            None,
            {"decision": "approved", "processing_time_bucket": "same_day"},
        ),
    )
    total_events = 100_000
    batch_size = 5_000
    for batch_start in range(0, total_events, batch_size):
        rows = []
        for index in range(batch_start, batch_start + batch_size):
            event_name, module_key, duration_ms, dimensions = event_specs[
                index % len(event_specs)
            ]
            occurred_at = NOW - timedelta(
                days=index % 90,
                seconds=index % 86_400,
            )
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "event_id": uuid.uuid4(),
                    "event_name": event_name,
                    "module_key": module_key,
                    "actor_user_id": actor.id,  # type: ignore[union-attr]
                    "organization_unit_id": unit.id,  # type: ignore[union-attr]
                    "role_snapshot": "admin",
                    "request_id": f"performance-{index}",
                    "outcome": "succeeded",
                    "duration_ms": duration_ms,
                    "dimensions": dimensions,
                    "occurred_at": occurred_at,
                    "created_at": occurred_at,
                }
            )
        db.execute(insert(ProductEvent), rows)
    db.flush()
    db.execute(text("ANALYZE product_events"))

    service = AnalyticsService()
    connection = db.connection()
    query_count = 0

    def count_query(*_: object) -> None:
        nonlocal query_count
        query_count += 1

    def run_suite(window_days: int) -> tuple[float, int]:
        nonlocal query_count
        query_count = 0
        window = resolve_window(NOW - timedelta(days=window_days), NOW, now=NOW)
        started = time.perf_counter()
        for method in (
            service.overview,
            service.knowledge,
            service.hr_funnel,
            service.tools,
            service.workflows,
        ):
            method(db, actor, window, None)  # type: ignore[arg-type]
        return (time.perf_counter() - started) * 1_000, query_count

    sqlalchemy_event.listen(connection, "before_cursor_execute", count_query)
    try:
        run_suite(90)
        seven_day_samples = [run_suite(7) for _ in range(5)]
        ninety_day_samples = [run_suite(90) for _ in range(5)]
    finally:
        sqlalchemy_event.remove(connection, "before_cursor_execute", count_query)

    seven_day_ms = [sample[0] for sample in seven_day_samples]
    ninety_day_ms = [sample[0] for sample in ninety_day_samples]
    explain = connection.execute(
        text(
            "EXPLAIN (FORMAT JSON) "
            "SELECT event_name, count(*) FROM product_events "
            "WHERE occurred_at >= :start AND occurred_at < :end "
            "GROUP BY event_name"
        ),
        {"start": NOW - timedelta(days=90), "end": NOW},
    ).scalar_one()[0]["Plan"]
    report = {
        "synthetic_event_rows": total_events,
        "seven_day": {
            "query_count": seven_day_samples[0][1],
            "p50_ms": _nearest_rank(seven_day_ms, 0.50),
            "p95_ms": _nearest_rank(seven_day_ms, 0.95),
        },
        "ninety_day": {
            "query_count": ninety_day_samples[0][1],
            "p50_ms": _nearest_rank(ninety_day_ms, 0.50),
            "p95_ms": _nearest_rank(ninety_day_ms, 0.95),
        },
        "explain": {
            "node_type": explain["Node Type"],
            "plan_rows": explain["Plan Rows"],
            "total_cost": explain["Total Cost"],
        },
    }
    output_dir = Path(__file__).resolve().parents[3] / "output" / "workbench"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"analytics-performance-{uuid.uuid4().hex}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    assert report["seven_day"]["p95_ms"] <= 500
    assert report["ninety_day"]["p95_ms"] <= 1_500


def test_procurement_funnel_meets_100k_audit_budget_with_plan_evidence(
    analytics_session: tuple[Session, dict[str, object]],
    tmp_path: Path,
) -> None:
    db, values = analytics_session
    actor = values["global"]
    unit = values["unit_a"]
    assert isinstance(actor, User)
    assert isinstance(unit, OrganizationUnit)

    event_specs = (
        ("procurement_request_submitted", "succeeded", "submission"),
        ("approval_task_approved", "succeeded", "department_review"),
        ("approval_task_approved", "succeeded", "procurement_review"),
        ("approval_task_rejected", "succeeded", "department_review"),
        ("procurement_request_withdrawn", "succeeded", "withdrawal"),
        ("procurement_permission_denied", "denied", "submission"),
        ("procurement_operation_conflict", "conflict", "department_review"),
        ("procurement_operation_replayed", "replayed", "procurement_review"),
    )
    total_audits = 100_000
    request_ids = [uuid.uuid4() for _ in range(total_audits // 2)]
    batch_size = 5_000
    for batch_start in range(0, total_audits, batch_size):
        rows = []
        for index in range(batch_start, batch_start + batch_size):
            event_name, outcome, stage = event_specs[index % len(event_specs)]
            request_id = request_ids[index // 2]
            occurred_at = NOW - timedelta(
                days=index % 90,
                seconds=index % 86_400,
            )
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "event_name": event_name,
                    "actor_user_id": actor.id,
                    "target_type": "procurement_request",
                    "target_id": request_id,
                    "operation_id": uuid.uuid4(),
                    "outcome": outcome,
                    "request_id": str(uuid.uuid4()),
                    "summary": {
                        "procurement_request_id": str(request_id),
                        "organization_unit_id": str(unit.id),
                        "stage": stage,
                    },
                    "occurred_at": occurred_at,
                    "created_at": occurred_at,
                }
            )
        db.execute(insert(SecurityAuditEvent), rows)
    db.flush()

    service = AnalyticsService()
    connection = db.connection()
    query_count = 0

    def count_query(*_: object) -> None:
        nonlocal query_count
        query_count += 1

    def run_funnel(window_days: int) -> tuple[float, int]:
        nonlocal query_count
        query_count = 0
        window = resolve_window(
            NOW - timedelta(days=window_days), NOW, now=NOW
        )
        started = time.perf_counter()
        service.procurement_funnel(db, actor, window, None)
        return (time.perf_counter() - started) * 1_000, query_count

    sqlalchemy_event.listen(connection, "before_cursor_execute", count_query)
    try:
        run_funnel(90)
        seven_day_samples = [run_funnel(7) for _ in range(5)]
        ninety_day_samples = [run_funnel(90) for _ in range(5)]
    finally:
        sqlalchemy_event.remove(connection, "before_cursor_execute", count_query)

    explain = connection.execute(
        text(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
            "SELECT count(*) FROM ("
            "SELECT target_id FROM security_audit_events "
            "WHERE occurred_at >= :start AND occurred_at < :end "
            "AND target_id IS NOT NULL "
            "AND (event_name = 'procurement_request_submitted' "
            "OR event_name IN ('approval_task_rejected', "
            "'procurement_request_withdrawn') "
            "OR (event_name = 'approval_task_approved' "
            "AND summary->>'stage' = 'procurement_review')) "
            "GROUP BY target_id HAVING "
            "count(id) FILTER (WHERE event_name = "
            "'procurement_request_submitted') > 0 "
            "AND count(id) FILTER (WHERE event_name IN "
            "('approval_task_rejected', 'procurement_request_withdrawn') "
            "OR (event_name = 'approval_task_approved' "
            "AND summary->>'stage' = 'procurement_review')) > 0"
            ") AS audited_requests"
        ),
        {"start": NOW - timedelta(days=90), "end": NOW},
    ).scalar_one()[0]["Plan"]

    def plan_nodes(plan: dict[str, object]) -> list[dict[str, object]]:
        return [plan, *[
            node
            for child in plan.get("Plans", [])
            for node in plan_nodes(child)
        ]]

    nodes = plan_nodes(explain)
    report = {
        "synthetic_audit_rows": total_audits,
        "seven_day": {
            "query_count": seven_day_samples[0][1],
            "p50_ms": _nearest_rank(
                [sample[0] for sample in seven_day_samples], 0.50
            ),
            "p95_ms": _nearest_rank(
                [sample[0] for sample in seven_day_samples], 0.95
            ),
        },
        "ninety_day": {
            "query_count": ninety_day_samples[0][1],
            "p50_ms": _nearest_rank(
                [sample[0] for sample in ninety_day_samples], 0.50
            ),
            "p95_ms": _nearest_rank(
                [sample[0] for sample in ninety_day_samples], 0.95
            ),
        },
        "explain": {
            "node_type": explain["Node Type"],
            "actual_total_time_ms": explain["Actual Total Time"],
            "plan_rows": explain["Plan Rows"],
            "total_cost": explain["Total Cost"],
            "scan_nodes": [
                {
                    "node_type": node["Node Type"],
                    "relation": node.get("Relation Name"),
                    "actual_rows": node.get("Actual Rows"),
                    "shared_hit_blocks": node.get("Shared Hit Blocks"),
                    "shared_read_blocks": node.get("Shared Read Blocks"),
                }
                for node in nodes
                if "Scan" in str(node["Node Type"])
            ],
        },
    }
    report_path = tmp_path / "procurement-funnel-performance.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))

    assert len(report["explain"]["scan_nodes"]) == 1
    assert not any("Join" in str(node["Node Type"]) for node in nodes)
    assert report["seven_day"]["query_count"] <= 8
    assert report["ninety_day"]["query_count"] <= 8
    assert report["seven_day"]["p95_ms"] <= 500
    assert report["ninety_day"]["p95_ms"] <= 1_500
