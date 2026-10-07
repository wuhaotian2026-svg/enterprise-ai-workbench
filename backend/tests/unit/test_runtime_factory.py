import httpx
import pytest
from pydantic import ValidationError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import uuid

import policy_api.procurement.runtime as procurement_runtime_module
import policy_api.main as main_module
from policy_api.main import create_production_app
from policy_api.answers.llm_client import AnswerModelClient
from policy_api.ingestion.embedding_client import EmbeddingClient
from policy_api.hr.runtime import HrRuntime, _refuse_policy_search
from policy_api.tools.planner_client import ToolPlanningClient
from policy_api.tools.schemas import TextBlock, ToolTurnResponse
from policy_api.slot_extraction.client import SlotExtractionClient
from policy_api.slot_extraction.repository import SlotExtractionOperationRepository
from policy_api.slot_extraction.service import SlotExtractionService
from policy_api.workbench.capabilities import CapabilityResolver
from policy_api.workbench.catalog import ModuleCatalog
from policy_api.workbench.runtime import WorkbenchRuntime
from policy_api.approvals.runtime import ApprovalRuntime
from policy_api.approvals.enums import ApprovalInstanceStatus
from policy_api.procurement.runtime import (
    ApprovalApiRuntime,
    ProcurementRuntime,
    SqlAlchemyApprovalTaskReader,
)
from policy_api.approvals.schemas import ApprovalTaskSummary, SubjectSummary
from policy_api.tools.errors import ToolError
from sqlalchemy.engine import Engine


@pytest.mark.anyio
async def test_production_factory_loads_settings_and_wires_database(monkeypatch) -> None:
    planner_close_calls: list[ToolPlanningClient] = []
    slot_close_calls: list[SlotExtractionClient] = []

    def close_planner_once(client: ToolPlanningClient) -> None:
        planner_close_calls.append(client)

    monkeypatch.setattr(ToolPlanningClient, "close", close_planner_once)
    monkeypatch.setattr(
        SlotExtractionClient,
        "close",
        lambda client: slot_close_calls.append(client),
    )
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/app")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("MODEL_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("CHAT_MODEL", "test-chat")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embedding")

    app = create_production_app()

    assert app.state.session_factory is not None
    assert callable(app.state.answer_question)
    assert app.state.hr_runtime is not None
    assert app.state.hr_runtime.search_policy is not _refuse_policy_search
    assert app.state.hr_runtime.max_model_calls == 3
    assert app.state.hr_runtime.max_read_calls == 4
    assert app.state.hr_runtime.confirmation_ttl_seconds == 600
    assert isinstance(app.state.workbench_runtime, WorkbenchRuntime)
    assert isinstance(app.state.procurement_runtime, ProcurementRuntime)
    assert isinstance(app.state.procurement_runtime.planner, ToolPlanningClient)
    assert app.state.procurement_runtime.planner is not app.state.hr_runtime.planner
    assert app.state.procurement_runtime.approval_runtime is app.state.approval_runtime
    assert app.state.procurement_runtime.search_policy is app.state.hr_runtime.search_policy
    assert app.state.procurement_runtime.max_model_calls == 3
    assert app.state.procurement_runtime.max_read_calls == 4
    assert app.state.procurement_runtime.confirmation_ttl_seconds == 600
    assert isinstance(app.state.hr_runtime.slot_extraction, SlotExtractionService)
    assert (
        app.state.hr_runtime.slot_extraction
        is app.state.procurement_runtime.slot_extraction
    )
    assert (
        app.state.hr_runtime.slot_extraction.client
        is app.state.procurement_runtime.slot_extraction.client
    )
    assert app.state.hr_runtime.slot_extraction.client.model == "test-chat"
    assert (
        app.state.procurement_runtime.request_reader.__class__.__name__
        == "SqlAlchemyProcurementRequestReader"
    )
    assert isinstance(app.state.approval_runtime, ApprovalApiRuntime)
    assert isinstance(app.state.approval_runtime.core_runtime, ApprovalRuntime)
    assert isinstance(
        app.state.approval_runtime.reader,
        SqlAlchemyApprovalTaskReader,
    )
    assert (
        app.state.procurement_runtime.capability_resolver
        is app.state.hr_runtime.capability_resolver
    )
    assert isinstance(
        app.state.workbench_runtime.capability_resolver,
        CapabilityResolver,
    )
    assert isinstance(app.state.workbench_runtime.module_catalog, ModuleCatalog)
    assert app.state.workbench_runtime.hr_runtime is app.state.hr_runtime
    assert (
        app.state.hr_runtime.capability_resolver
        is app.state.workbench_runtime.capability_resolver
    )
    assert (
        app.state.hr_runtime.product_event_emitter
        is app.state.workbench_runtime.product_event_emitter
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/hr/conversations")).status_code == 401
    assert app.state.ingestion_worker is not None
    assert not app.state.ingestion_worker.is_alive
    assert app.state.close_runtime in app.router.on_shutdown
    app.state.close_runtime()
    app.state.close_runtime()
    assert planner_close_calls == [
        app.state.procurement_runtime.planner,
        app.state.hr_runtime.planner,
    ]
    assert len({id(item) for item in planner_close_calls}) == 2
    assert slot_close_calls == [app.state.hr_runtime.slot_extraction.client]


def test_tool_turn_response_tracks_extraction_without_expanding_planner_budget() -> None:
    response = ToolTurnResponse(
        blocks=(TextBlock(text="ok"),),
        model_calls=3,
        read_calls=4,
        write_proposals=1,
        slot_extraction_calls=1,
    )

    assert response.model_calls == 3
    assert response.slot_extraction_calls == 1
    assert ToolTurnResponse(
        blocks=(), model_calls=0, read_calls=0, write_proposals=0
    ).slot_extraction_calls == 0
    with pytest.raises(ValidationError):
        ToolTurnResponse(
            blocks=(),
            model_calls=0,
            read_calls=0,
            write_proposals=0,
            slot_extraction_calls=2,
        )


def test_production_startup_converges_stale_extractions_without_provider_dispatch(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/app")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("MODEL_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("CHAT_MODEL", "test-chat")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embedding")
    committed: list[bool] = []
    converged_at: list[datetime] = []

    class Database:
        def commit(self) -> None:
            committed.append(True)

    class SessionContext:
        def __enter__(self) -> Database:
            return Database()

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(
        main_module,
        "create_session_factory",
        lambda _engine: lambda: SessionContext(),
    )
    monkeypatch.setattr(
        SlotExtractionOperationRepository,
        "converge_stale",
        lambda _repository, _db, *, now, operation_id=None: (
            converged_at.append(now),
            1,
        )[1],
    )
    monkeypatch.setattr(
        SlotExtractionClient,
        "extract",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("startup must not dispatch slot extraction")
        ),
    )

    app = create_production_app()

    assert len(app.router.on_startup) == 1
    app.router.on_startup[0]()
    assert len(converged_at) == 1
    assert converged_at[0].tzinfo is not None
    assert committed == [True]
    app.state.close_runtime()


@pytest.mark.parametrize(
    "failed_name",
    (
        "worker",
        "procurement_planner",
        "hr_runtime",
        "slot_extraction",
        "embedding",
        "answer",
        "engine",
    ),
)
def test_production_shutdown_continues_after_failure_and_retries_only_failed_resource(
    monkeypatch, failed_name: str,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/app")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("MODEL_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("CHAT_MODEL", "test-chat")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embedding")
    app = create_production_app()
    engine = app.state.session_factory.kw["bind"]
    calls: list[str] = []
    fail_once = {failed_name}

    def closer(name: str):  # type: ignore[no-untyped-def]
        def close(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(name)
            if name in fail_once:
                fail_once.remove(name)
                raise RuntimeError(f"{name}-failed")
        return close

    monkeypatch.setattr(app.state.ingestion_worker, "stop", closer("worker"))
    monkeypatch.setattr(
        app.state.procurement_runtime.planner,
        "close",
        closer("procurement_planner"),
    )
    original_hr_close = HrRuntime.close

    def close_hr_runtime(runtime: HrRuntime) -> None:
        if runtime is app.state.hr_runtime:
            closer("hr_runtime")()
            return
        original_hr_close(runtime)

    monkeypatch.setattr(HrRuntime, "close", close_hr_runtime)
    monkeypatch.setattr(
        SlotExtractionClient,
        "close",
        lambda _client: closer("slot_extraction")(),
    )
    monkeypatch.setattr(EmbeddingClient, "close", closer("embedding"))
    monkeypatch.setattr(AnswerModelClient, "close", closer("answer"))
    monkeypatch.setattr(engine, "dispose", closer("engine"))

    with pytest.raises(RuntimeError, match=f"{failed_name}-failed"):
        app.state.close_runtime()
    assert calls == [
        "worker", "procurement_planner", "hr_runtime",
        "slot_extraction", "embedding", "answer", "engine",
    ]

    app.state.close_runtime()
    assert calls.count(failed_name) == 2
    for name in {
        "worker", "procurement_planner", "hr_runtime",
        "slot_extraction", "embedding", "answer", "engine",
    } - {failed_name}:
        assert calls.count(name) == 1


def test_production_factory_failure_closes_all_already_owned_resources(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/app")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("MODEL_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("CHAT_MODEL", "test-chat")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embedding")
    calls: list[str] = []
    planner_ids: list[int] = []
    monkeypatch.setattr(
        ToolPlanningClient,
        "close",
        lambda self: (calls.append("planner"), planner_ids.append(id(self))),
    )
    monkeypatch.setattr(EmbeddingClient, "close", lambda self: calls.append("embedding"))
    monkeypatch.setattr(AnswerModelClient, "close", lambda self: calls.append("answer"))
    monkeypatch.setattr(
        SlotExtractionClient,
        "close",
        lambda self: calls.append("slot_extraction"),
    )
    monkeypatch.setattr(Engine, "dispose", lambda self: calls.append("engine"))
    monkeypatch.setattr(
        main_module,
        "create_app",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("create-app-failed")),
    )

    with pytest.raises(RuntimeError, match="create-app-failed"):
        create_production_app()

    assert calls == [
        "planner", "planner", "slot_extraction", "answer", "embedding", "engine"
    ]
    assert len(set(planner_ids)) == 2


def test_production_factory_preserves_constructor_error_when_cleanup_fails(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/app")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("MODEL_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("CHAT_MODEL", "test-chat")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embedding")
    calls: list[str] = []
    monkeypatch.setattr(
        ToolPlanningClient,
        "close",
        lambda self: calls.append("planner"),
    )
    monkeypatch.setattr(
        EmbeddingClient,
        "close",
        lambda self: calls.append("embedding"),
    )
    monkeypatch.setattr(
        AnswerModelClient,
        "close",
        lambda self: calls.append("answer"),
    )
    monkeypatch.setattr(
        SlotExtractionClient,
        "close",
        lambda self: calls.append("slot_extraction"),
    )

    def fail_engine_cleanup(_engine: Engine) -> None:
        calls.append("engine")
        raise RuntimeError("engine-cleanup-failed")

    monkeypatch.setattr(Engine, "dispose", fail_engine_cleanup)
    monkeypatch.setattr(
        main_module,
        "create_app",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("create-app-failed")
        ),
    )

    with pytest.raises(ValueError, match="create-app-failed") as raised:
        create_production_app()

    assert calls == [
        "planner", "planner", "slot_extraction", "answer", "embedding", "engine"
    ]
    assert any(
        "engine-cleanup-failed" in note
        for note in getattr(raised.value, "__notes__", ())
    )


def test_production_shutdown_retries_failed_hr_planner_close(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/app")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("MODEL_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("CHAT_MODEL", "test-chat")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embedding")
    app = create_production_app()
    hr_planner = app.state.hr_runtime.planner
    calls: list[ToolPlanningClient] = []
    failed = False

    def close_planner(planner: ToolPlanningClient) -> None:
        nonlocal failed
        calls.append(planner)
        if planner is hr_planner and not failed:
            failed = True
            raise RuntimeError("hr-planner-failed")

    monkeypatch.setattr(ToolPlanningClient, "close", close_planner)

    with pytest.raises(RuntimeError, match="hr-planner-failed"):
        app.state.close_runtime()
    app.state.close_runtime()

    assert calls.count(hr_planner) == 2
    assert calls.count(app.state.procurement_runtime.planner) == 1


def test_approval_api_facade_consumes_only_max_scan_plus_one_candidates() -> None:
    consumed = 0
    summary = SubjectSummary(
        request_number="PR-BOUND", title="Bound", total=Decimal("1.00"),
        status="pending_manager",
    )

    class Reader:
        def candidates(
            self, _db, *, actor, status, process_key,
            activated_from, activated_to,
        ):
            nonlocal consumed
            for _index in range(10):
                consumed += 1
                if consumed > 3:
                    raise AssertionError("reader consumed beyond max_scan + 1")
                yield ApprovalTaskSummary(
                    task_id=uuid.uuid4(), instance_id=uuid.uuid4(),
                    process_key="procurement.request", subject_type="procurement_request",
                    step_key="department_manager_review", step_label="Manager",
                    status="pending", submitted_at=datetime.now(timezone.utc),
                    activated_at=datetime.now(timezone.utc), completed_at=None,
                    subject=summary,
                )

        def summary(self, _db, *, actor, candidate):
            return candidate

    facade = ApprovalApiRuntime(object(), reader=Reader(), max_scan=2)
    with pytest.raises(ToolError, match="approval_page_limit_exceeded"):
        facade.list_tasks(
            object(), actor=object(), status="pending", process_key=None,
            activated_from=None, activated_to=None, offset=0, limit=20,
        )
    assert consumed == 3


@pytest.mark.parametrize("visible, expected_adapter_calls", [(False, 0), (True, 2)])
def test_production_approval_reader_bounds_raw_candidates_and_closes_result(
    visible: bool,
    expected_adapter_calls: int,
) -> None:
    reader_type = getattr(
        procurement_runtime_module,
        "SqlAlchemyApprovalTaskReader",
        None,
    )
    assert reader_type is not None, "production approval reader port is missing"

    class ClosingResult:
        def __init__(self, rows):
            self._rows = iter(rows)
            self.consumed = 0
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            value = next(self._rows)
            self.consumed += 1
            return value

        def close(self):
            self.closed = True

    now = datetime.now(timezone.utc)
    rows = []
    for _index in range(10):
        task = SimpleNamespace(
            id=uuid.uuid4(), instance_id=uuid.uuid4(),
            step_key="department_manager_review", step_label="Manager",
            status=SimpleNamespace(value="pending"), activated_at=now,
            completed_at=None,
        )
        instance = SimpleNamespace(
            id=task.instance_id, process_key="procurement.request",
            subject_type="procurement_request", submitted_at=now,
        )
        rows.append((task, instance, None))
    result = ClosingResult(rows)

    class Database:
        def __init__(self):
            self.statement = None

        def execute(self, statement):
            self.statement = statement
            return result

    class Access:
        def __init__(self):
            self.calls = 0
            self.read_context_calls = 0

        def read_context(self, _db, *, actor):
            self.read_context_calls += 1
            return procurement_runtime_module.ProcurementApprovalReadContext(
                actor_id=actor.id,
                actor_is_active_employee=True,
                department_review_scope=None,
                final_review_scope=None,
            )

        def can_view(self, *_args, **_kwargs):
            self.calls += 1
            return visible

    class Registry:
        def __init__(self):
            self.calls = 0

        def summary(self, *_args, **_kwargs):
            self.calls += 1
            return SubjectSummary(
                request_number="PR-BOUND", title="Bound",
                total=Decimal("1.00"), status="pending_manager",
            )

    access = Access()
    registry = Registry()
    reader = reader_type(access=access, registry=registry)
    facade = ApprovalApiRuntime(object(), reader=reader, max_scan=2)
    database = Database()

    with pytest.raises(ToolError, match="approval_page_limit_exceeded"):
        facade.list_tasks(
            database, actor=SimpleNamespace(id=uuid.uuid4()), status="pending",
            process_key="procurement.request",
            activated_from=now - timedelta(days=1),
            activated_to=now + timedelta(days=1),
            offset=0, limit=20,
        )

    sql = str(database.statement)
    assert "approval_instances.process_key =" in sql
    assert "approval_tasks.status =" in sql
    assert "approval_tasks.activated_at >=" in sql
    assert "approval_tasks.activated_at <=" in sql
    assert "approval_decisions" not in sql
    assert result.consumed == 3
    assert access.read_context_calls == 1
    assert access.calls == 2
    assert registry.calls == expected_adapter_calls
    assert result.closed is True


def test_default_approval_scan_ceiling_is_conservative_and_bounded() -> None:
    ceiling = getattr(
        procurement_runtime_module,
        "DEFAULT_APPROVAL_RAW_SCAN_LIMIT",
        None,
    )
    assert ceiling == 200

    consumed = 0
    projected = 0
    closed = False

    class Reader:
        def candidates(
            self, _db, *, actor, status, process_key,
            activated_from, activated_to,
        ):
            nonlocal consumed, closed
            try:
                for index in range(ceiling + 10):
                    consumed += 1
                    yield index
            finally:
                closed = True

        def summary(self, _db, *, actor, candidate):
            nonlocal projected
            projected += 1
            return None

    facade = ApprovalApiRuntime(object(), reader=Reader())
    with pytest.raises(ToolError, match="approval_page_limit_exceeded"):
        facade.list_tasks(
            object(), actor=object(), status=None, process_key=None,
            activated_from=None, activated_to=None, offset=0, limit=20,
        )

    assert consumed == ceiling + 1
    assert projected == ceiling
    assert closed is True


def test_procurement_reader_uses_owner_count_and_bounded_page_without_full_service_list() -> None:
    reader_type = getattr(
        procurement_runtime_module,
        "SqlAlchemyProcurementRequestReader",
        None,
    )
    assert reader_type is not None, "production procurement reader port is missing"

    actor_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    instance_id = uuid.uuid4()
    now = datetime(2030, 1, 2, tzinfo=timezone.utc)
    actor = SimpleNamespace(id=actor_id, is_active=True)
    profile = SimpleNamespace(id=profile_id, user_id=actor_id, is_active=True)
    instance = SimpleNamespace(
        id=instance_id,
        subject_type="procurement_request",
        process_key="procurement.request",
        process_version=1,
        applicant_user_id=actor_id,
        organization_unit_id=organization_id,
        status=ApprovalInstanceStatus.RUNNING,
        current_step_key="department_manager_review",
    )
    request = SimpleNamespace(
        id=uuid.uuid4(),
        request_number="PR-PAGED",
        approval_instance_id=instance_id,
        applicant_employee_id=profile_id,
        organization_unit_id=organization_id,
        title="Paged",
        total_amount=Decimal("10.00"),
        submitted_at=now,
    )

    class Repository:
        def get_active_user(self, _db, user_id):
            assert user_id == actor_id
            return actor

        def get_active_profile_by_user(self, _db, user_id):
            assert user_id == actor_id
            return profile

    class Database:
        def __init__(self):
            self.statements = []

        def scalar(self, statement):
            self.statements.append(statement)
            return 1000

        def execute(self, statement):
            self.statements.append(statement)
            return [(request, instance)]

    class Service:
        def list_my_requests(self, *_args, **_kwargs):
            raise AssertionError("full service list must not be called")

    database = Database()
    reader = reader_type(repository=Repository())
    runtime = ProcurementRuntime(
        service=Service(),
        capability_resolver=object(),
        request_reader=reader,
    )
    page = runtime.list_requests(
        database,
        actor=actor,
        status="pending_manager",
        submitted_from=date(2030, 1, 1),
        submitted_to=date(2030, 1, 3),
        offset=7,
        limit=1,
    )

    assert page["total"] == 1000
    assert len(page["items"]) == 1
    assert page["items"][0]["id"] == request.id
    assert len(database.statements) == 2
    count_sql = str(database.statements[0])
    page_sql = str(database.statements[1])
    for sql in (count_sql, page_sql):
        assert "procurement_requests.applicant_employee_id" in sql
        assert "approval_instances.applicant_user_id" in sql
        assert "procurement_requests.submitted_at >=" in sql
        assert "procurement_requests.submitted_at <" in sql
        assert "approval_instances.current_step_key" in sql
    assert "ORDER BY procurement_requests.submitted_at DESC" in page_sql
    assert "LIMIT" in page_sql and "OFFSET" in page_sql
    assert database.statements[1]._limit_clause.value == 1
    assert database.statements[1]._offset_clause.value == 7


def test_procurement_reader_treats_date_max_as_an_unbounded_inclusive_upper_date() -> None:
    actor_id = uuid.uuid4()
    actor = SimpleNamespace(id=actor_id)
    profile = SimpleNamespace(id=uuid.uuid4())

    class Repository:
        def get_active_user(self, _db, user_id):
            assert user_id == actor_id
            return actor

        def get_active_profile_by_user(self, _db, user_id):
            assert user_id == actor_id
            return profile

    class Database:
        def __init__(self):
            self.statements = []

        def scalar(self, statement):
            self.statements.append(statement)
            return 0

        def execute(self, statement):
            self.statements.append(statement)
            return []

    database = Database()
    reader = procurement_runtime_module.SqlAlchemyProcurementRequestReader(
        repository=Repository()
    )
    page = reader.page(
        database,
        actor=actor,
        status=None,
        submitted_from=date(2030, 1, 1),
        submitted_to=date.max,
        offset=0,
        limit=1,
    )

    assert page == {"items": [], "offset": 0, "limit": 1, "total": 0}
    assert len(database.statements) == 2
    for statement in database.statements:
        sql = str(statement)
        assert "procurement_requests.submitted_at >=" in sql
        assert "procurement_requests.submitted_at <" not in sql
