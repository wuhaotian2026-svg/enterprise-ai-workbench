from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import time
from typing import Any
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from policy_api.approvals.enums import (
    ApprovalInstanceStatus,
    ApprovalTaskStatus,
    AssignmentKind,
)
from policy_api.approvals.models import ApprovalInstance, ApprovalTask
from policy_api.approvals.runtime import ApprovalRuntime
from policy_api.approvals.schemas import (
    SubjectApplicant,
    SubjectDetail,
    SubjectOrganization,
    SubjectSummary,
)
from policy_api.hr.models import EmployeeProfile
from policy_api.models import User, UserRole
from policy_api.procurement.runtime import (
    ApprovalApiRuntime,
    SqlAlchemyApprovalTaskReader,
)
from policy_api.procurement.service import (
    PROCUREMENT_REQUEST_V1,
    SUBJECT_TYPE,
    ProcurementApprovalAccess,
)
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityResolver,
    OrganizationUnit,
    ScopeKind,
)


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
NOW = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
TOTAL_TASKS = 100_000
P95_SAMPLE_COUNT = 20


class _SummaryRegistry:
    def summary(
        self,
        _subject_type: str,
        _db: Session,
        _instance: ApprovalInstance,
    ) -> SubjectSummary:
        return SubjectSummary(
            request_number="PR-PERF",
            title="Performance fixture",
            total=Decimal("1.00"),
            status="running",
        )

    def detail(
        self,
        _subject_type: str,
        _db: Session,
        _instance: ApprovalInstance,
    ) -> SubjectDetail:
        return SubjectDetail(
            purpose="Performance fixture",
            needed_by_date=date(2026, 9, 1),
            currency="CNY",
            items=(),
            applicant=SubjectApplicant(display_name="Performance actor"),
            organization=SubjectOrganization(display_name="Performance child"),
            timeline=(),
        )


class _AllowAuthorization:
    def authorize(self, **_kwargs: object) -> None:
        return None


class _WriteAccess:
    def authorization(self, _db: Session) -> _AllowAuthorization:
        return _AllowAuthorization()

    def can_view(self, *_args: object, **_kwargs: object) -> bool:
        return True


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


def _plan_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        plan,
        *[
            node
            for child in plan.get("Plans", [])
            for node in _plan_nodes(child)
        ],
    ]


@pytest.fixture(scope="module")
def approval_performance_session() -> tuple[Session, dict[str, object]]:
    if not TEST_DATABASE_URL:
        pytest.fail("TEST_DATABASE_URL is required for approval performance tests")
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
        actor = User(
            username=f"approval-perf-actor-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        other = User(
            username=f"approval-perf-other-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        root = OrganizationUnit(
            code=f"APPROVAL-PERF-ROOT-{suffix.upper()}",
            name="Approval performance root",
            is_active=True,
        )
        child = OrganizationUnit(
            code=f"APPROVAL-PERF-CHILD-{suffix.upper()}",
            name="Approval performance child",
            parent_id=root.id,
            is_active=True,
        )
        other_unit = OrganizationUnit(
            code=f"APPROVAL-PERF-OTHER-{suffix.upper()}",
            name="Approval performance other",
            is_active=True,
        )
        db.add_all([actor, other, root])
        db.flush()
        child.parent_id = root.id
        db.add_all([child, other_unit])
        db.flush()
        db.add(
            EmployeeProfile(
                user_id=actor.id,
                employee_number=f"PERF-{suffix[:12]}",
                display_name="Approval performance actor",
                department="Performance",
                organization_unit_id=root.id,
                hire_date=date(2020, 1, 1),
                is_active=True,
            )
        )
        db.add_all(
            [
                CapabilityGrant(
                    user_id=actor.id,
                    capability=Capability.PROCUREMENT_DEPARTMENT_REVIEW.value,
                    scope_kind=ScopeKind.UNIT_SUBTREE.value,
                    organization_unit_id=root.id,
                    is_active=True,
                ),
                CapabilityGrant(
                    user_id=actor.id,
                    capability=Capability.PROCUREMENT_FINAL_REVIEW.value,
                    scope_kind=ScopeKind.UNIT_SUBTREE.value,
                    organization_unit_id=root.id,
                    is_active=True,
                ),
            ]
        )
        db.flush()
        params = {
            "actor_id": actor.id,
            "other_id": other.id,
            "root_id": root.id,
            "child_id": child.id,
            "other_unit_id": other_unit.id,
            "now": NOW,
        }
        db.execute(
            text(
                "INSERT INTO approval_instances ("
                "id, process_key, process_version, subject_type, applicant_user_id, "
                "organization_unit_id, status, current_step_key, version, submitted_at, "
                "completed_at, created_at, updated_at) "
                "SELECT md5('approval-perf-instance-' || n::text)::uuid, "
                ":process_key, :process_version, :subject_type, :actor_id, "
                "CASE WHEN n % 10000 IN (0, 1, 2, 3) THEN :child_id "
                "ELSE :other_unit_id END, "
                "CASE WHEN n % 4 IN (0, 1) THEN 'running' ELSE 'approved' END, "
                "CASE WHEN n % 4 IN (0, 1) THEN "
                "CASE WHEN n % 4 = 0 THEN 'department_manager_review' "
                "ELSE 'procurement_review' END ELSE NULL END, 1, "
                ":now - ((n % 86400) * interval '1 second'), "
                "CASE WHEN n % 4 IN (2, 3) THEN "
                ":now - ((n % 86400) * interval '1 second') ELSE NULL END, "
                ":now, :now FROM generate_series(1, :total) AS n"
            ),
            {
                **params,
                "total": TOTAL_TASKS,
                "process_key": PROCUREMENT_REQUEST_V1.process_key,
                "process_version": PROCUREMENT_REQUEST_V1.version,
                "subject_type": SUBJECT_TYPE,
            },
        )
        db.execute(
            text(
                "INSERT INTO approval_tasks ("
                "id, instance_id, sequence, step_key, step_label, assignment_kind, "
                "assigned_user_id, required_capability, scope_organization_unit_id, "
                "status, activated_at, completed_at, created_at, updated_at) "
                "SELECT md5('approval-perf-task-' || n::text)::uuid, "
                "md5('approval-perf-instance-' || n::text)::uuid, 1, "
                "CASE WHEN n % 2 = 0 THEN 'department_manager_review' "
                "ELSE 'procurement_review' END, "
                "CASE WHEN n % 2 = 0 THEN 'Department review' ELSE 'Procurement review' END, "
                "CASE WHEN n % 2 = 0 THEN 'user' ELSE 'capability' END, "
                "CASE WHEN n % 2 = 0 THEN "
                "CASE WHEN n % 10000 IN (0, 2) THEN :actor_id ELSE :other_id END "
                "ELSE NULL END, "
                "CASE WHEN n % 2 = 1 THEN :final_capability ELSE NULL END, "
                "CASE WHEN n % 2 = 1 THEN "
                "CASE WHEN n % 10000 IN (1, 3) THEN :child_id ELSE :other_unit_id END "
                "ELSE NULL END, "
                "CASE WHEN n % 4 IN (0, 1) THEN 'pending' ELSE 'approved' END, "
                ":now - ((n % 86400) * interval '1 second'), "
                "CASE WHEN n % 4 IN (2, 3) THEN "
                ":now - ((n % 86400) * interval '1 second') ELSE NULL END, "
                ":now, :now FROM generate_series(1, :total) AS n"
            ),
            {
                **params,
                "total": TOTAL_TASKS,
                "final_capability": Capability.PROCUREMENT_FINAL_REVIEW.value,
            },
        )
        db.execute(
            text(
                "INSERT INTO approval_decisions ("
                "id, instance_id, task_id, actor_user_id, action, comment, "
                "client_operation_id, decided_at, created_at) "
                "SELECT md5('approval-perf-decision-' || n::text)::uuid, "
                "md5('approval-perf-instance-' || n::text)::uuid, "
                "md5('approval-perf-task-' || n::text)::uuid, "
                "CASE WHEN n % 10000 IN (2, 3) THEN :actor_id ELSE :other_id END, "
                "'approve', NULL, md5('approval-perf-operation-' || n::text)::uuid, "
                ":now - ((n % 86400) * interval '1 second'), :now "
                "FROM generate_series(1, :total) AS n WHERE n % 4 IN (2, 3)"
            ),
            {**params, "total": TOTAL_TASKS},
        )
        db.execute(text("ANALYZE approval_instances"))
        db.execute(text("ANALYZE approval_tasks"))
        db.execute(text("ANALYZE approval_decisions"))
        yield db, {
            "actor": actor,
            "root": root,
            "child": child,
            "other_unit": other_unit,
        }
    finally:
        db.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        engine.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def test_approval_queries_meet_100k_scope_and_latency_gates(
    approval_performance_session: tuple[Session, dict[str, object]],
    tmp_path: Path,
) -> None:
    db, values = approval_performance_session
    actor = values["actor"]
    root = values["root"]
    child = values["child"]
    assert isinstance(actor, User)
    assert isinstance(root, OrganizationUnit)
    assert isinstance(child, OrganizationUnit)

    access = ProcurementApprovalAccess(
        capability_resolver=CapabilityResolver(),
    )
    core_runtime = ApprovalRuntime(
        registry=_SummaryRegistry(),  # type: ignore[arg-type]
        access=access,  # type: ignore[arg-type]
        now_factory=lambda: NOW,
    )
    runtime = ApprovalApiRuntime(
        core_runtime,
        reader=SqlAlchemyApprovalTaskReader(
            access=access,
            registry=_SummaryRegistry(),  # type: ignore[arg-type]
        ),
    )

    def run(status: str) -> tuple[float, dict[str, object]]:
        started = time.perf_counter()
        response = runtime.list_tasks(
            db,
            actor=actor,
            status=status,
            process_key=PROCUREMENT_REQUEST_V1.process_key,
            activated_from=NOW - timedelta(days=2),
            activated_to=NOW,
            offset=0,
            limit=50,
        )
        return (time.perf_counter() - started) * 1_000, response

    run("pending")
    pending_samples = [run("pending") for _ in range(P95_SAMPLE_COUNT)]
    completed_statements: list[tuple[str, dict[str, object]]] = []
    connection = db.connection()

    def capture_completed_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "approval_tasks" in statement
            and isinstance(parameters, dict)
        ):
            completed_statements.append((statement, dict(parameters)))

    event.listen(connection, "before_cursor_execute", capture_completed_statement)
    try:
        run("approved")
    finally:
        event.remove(connection, "before_cursor_execute", capture_completed_statement)
    assert completed_statements
    completed_samples = [run("approved") for _ in range(P95_SAMPLE_COUNT)]

    visible_task_id = db.execute(
        text(
            "SELECT id FROM approval_tasks WHERE status = 'pending' "
            "AND assigned_user_id = :actor_id "
            "ORDER BY activated_at DESC NULLS LAST, id LIMIT 1"
        ),
        {"actor_id": actor.id},
    ).scalar_one()

    def get_detail() -> tuple[float, dict[str, object]]:
        started = time.perf_counter()
        response = runtime.get_task(
            db,
            actor=actor,
            task_id=visible_task_id,
        )
        return (time.perf_counter() - started) * 1_000, response

    get_detail()
    detail_samples = [get_detail() for _ in range(P95_SAMPLE_COUNT)]

    assert pending_samples[0][1]["total"] == 20
    assert completed_samples[0][1]["total"] == 20
    pending_p95 = _nearest_rank([sample[0] for sample in pending_samples], 0.95)
    completed_p95 = _nearest_rank([sample[0] for sample in completed_samples], 0.95)
    detail_p95 = _nearest_rank([sample[0] for sample in detail_samples], 0.95)

    completed_statement, completed_parameters = completed_statements[-1]
    completed_plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + completed_statement,
        completed_parameters,
    ).scalar_one()[0]["Plan"]
    assigned_plan = connection.execute(
        text(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
            "SELECT id FROM approval_tasks WHERE status = 'pending' "
            "AND assigned_user_id = :actor_id "
            "ORDER BY activated_at DESC NULLS LAST, id LIMIT 50"
        ),
        {"actor_id": actor.id},
    ).scalar_one()[0]["Plan"]
    capability_plan = connection.execute(
        text(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
            "SELECT id FROM approval_tasks WHERE status = 'pending' "
            "AND required_capability = :final_capability "
            "AND scope_organization_unit_id = :root_id "
            "ORDER BY activated_at DESC NULLS LAST, id LIMIT 50"
        ),
        {
            "root_id": child.id,
            "final_capability": Capability.PROCUREMENT_FINAL_REVIEW.value,
        },
    ).scalar_one()[0]["Plan"]
    assigned_indexes = {
        node.get("Index Name") for node in _plan_nodes(assigned_plan)
    }
    capability_indexes = {
        node.get("Index Name") for node in _plan_nodes(capability_plan)
    }
    completed_nodes = _plan_nodes(completed_plan)
    completed_indexes = {
        node.get("Index Name") for node in completed_nodes
    }
    completed_seq_scan_relations = {
        node.get("Relation Name")
        for node in completed_nodes
        if node.get("Node Type") == "Seq Scan"
    }
    assert "ix_approval_tasks_user_queue" in assigned_indexes
    assert (
        "ix_approval_tasks_capability_scope_queue" in capability_indexes
    )
    assert "ix_approval_tasks_user_queue" in completed_indexes
    assert "uq_approval_decision_actor_operation" in completed_indexes
    assert not completed_seq_scan_relations.intersection(
        {"approval_tasks", "approval_decisions"}
    )

    report = {
        "synthetic_approval_tasks": TOTAL_TASKS,
        "pending": {"samples_ms": [sample[0] for sample in pending_samples], "p95_ms": pending_p95},
        "completed": {"samples_ms": [sample[0] for sample in completed_samples], "p95_ms": completed_p95},
        "detail": {"samples_ms": [sample[0] for sample in detail_samples], "p95_ms": detail_p95},
        "assigned_plan": assigned_plan,
        "capability_plan": capability_plan,
        "completed_plan": completed_plan,
    }
    report_path = tmp_path / "approval-query-performance.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        "APPROVAL_QUERY_PERFORMANCE="
        + json.dumps(
            {
                "pending": report["pending"],
                "completed": report["completed"],
                "detail": report["detail"],
            },
            ensure_ascii=False,
            default=str,
        )
    )
    assert pending_p95 <= 500
    assert completed_p95 <= 500
    assert detail_p95 <= 500


def test_deterministic_approval_write_p95_is_below_one_second(
    approval_performance_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = approval_performance_session
    actor = values["actor"]
    root = values["root"]
    assert isinstance(actor, User)
    assert isinstance(root, OrganizationUnit)
    runtime = ApprovalRuntime(
        registry=object(),  # type: ignore[arg-type]
        access=_WriteAccess(),  # type: ignore[arg-type]
        now_factory=lambda: NOW,
    )
    samples: list[float] = []
    for index in range(P95_SAMPLE_COUNT):
        savepoint = db.begin_nested()
        instance = ApprovalInstance(
            id=uuid.uuid4(),
            process_key="performance_write_v1",
            process_version=1,
            subject_type="performance_write",
            applicant_user_id=actor.id,
            organization_unit_id=root.id,
            status=ApprovalInstanceStatus.RUNNING,
            current_step_key="review",
            version=1,
            submitted_at=NOW,
        )
        task = ApprovalTask(
            instance_id=instance.id,
            sequence=1,
            step_key="review",
            step_label="Review",
            assignment_kind=AssignmentKind.USER,
            assigned_user_id=actor.id,
            required_capability=None,
            scope_organization_unit_id=None,
            status=ApprovalTaskStatus.PENDING,
            activated_at=NOW,
        )
        db.add_all([instance, task])
        db.flush()
        started = time.perf_counter()
        runtime.approve_task(
            db,
            actor=actor,
            task_id=task.id,
            client_operation_id=uuid.uuid5(uuid.NAMESPACE_URL, f"approval-write-{index}"),
            comment="performance acceptance",
            commit=False,
        )
        db.flush()
        samples.append((time.perf_counter() - started) * 1_000)
        savepoint.rollback()
        db.expire_all()

    write_p95 = _nearest_rank(samples, 0.95)
    assert write_p95 <= 1_000
    print(
        "APPROVAL_WRITE_PERFORMANCE="
        + json.dumps(
            {"samples_ms": samples, "p95_ms": write_p95},
            ensure_ascii=False,
        )
    )
