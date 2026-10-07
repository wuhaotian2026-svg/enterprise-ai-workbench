from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timezone
import hmac
import time
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import text

from policy_api.auth.router import router as auth_router
from policy_api.auth.router import COOKIE_NAME, CSRF_COOKIE_NAME
from policy_api.config import Settings
from policy_api.documents.router import router as documents_router
from policy_api.errors import ErrorResponse
from policy_api.feedback.router import router as feedback_router
from policy_api.history.router import router as history_router
from policy_api.hr.permissions import HrPermissionError
from policy_api.hr.router import router as hr_router
from policy_api.hr.runtime import HrRuntime
from policy_api.knowledge.tools import PolicyCitation, PolicySearchOutcome
from policy_api.answers.schemas import AnswerOutcome
from policy_api.answers.llm_client import AnswerModelClient
from policy_api.answers.runtime import AnswerRuntime
from policy_api.approvals.router import router as approvals_router
from policy_api.approvals.runtime import ApprovalRuntime
from policy_api.approvals.service import ApprovalEngine
from policy_api.approvals.subject_adapter import SubjectAdapterRegistry
from policy_api.database import create_database_engine, create_session_factory
from policy_api.ingestion.embedding_client import EmbeddingClient
from policy_api.ingestion.worker import IngestionExecutor, IngestionWorker
from policy_api.observability import InvalidRequestId, log_event, normalize_request_id
from policy_api.procurement.repository import ProcurementRepository
from policy_api.procurement.observability import ProcurementObservability
from policy_api.procurement.router import router as procurement_router
from policy_api.procurement.runtime import (
    ApprovalApiRuntime,
    ProcurementRuntime,
    SqlAlchemyApprovalTaskReader,
    SqlAlchemyProcurementRequestReader,
)
from policy_api.procurement.service import (
    ProcurementApprovalAccess,
    ProcurementService,
    ProcurementSubjectAdapter,
)
from policy_api.rate_limit import FixedWindowRateLimiter
from policy_api.slot_extraction.client import SlotExtractionClient
from policy_api.slot_extraction.fingerprint import SlotExtractionFingerprinter
from policy_api.slot_extraction.repository import SlotExtractionOperationRepository
from policy_api.slot_extraction.service import SlotExtractionService
from policy_api.tools.planner_client import ToolPlanningClient
from policy_api.workbench.capabilities import CapabilityResolver
from policy_api.workbench.catalog import ModuleCatalog
from policy_api.workbench.events import ProductEventEmitter
from policy_api.workbench.router import workbench_router
from policy_api.workbench.runtime import WorkbenchRuntime


def create_app(settings: Settings, *, session_factory: sessionmaker[Session] | None = None,
               answer_question: Callable[[str], AnswerOutcome] | None = None,
               hr_runtime: object | None = None,
               workbench_runtime: WorkbenchRuntime | None = None,
               procurement_runtime: object | None = None,
               approval_runtime: object | None = None) -> FastAPI:
    production = settings.app_env == "production"
    app = FastAPI(title="Policy API", version="0.1.0",
        docs_url=None if production else "/docs", redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
    app.add_middleware(CORSMiddleware, allow_origins=settings.frontend_origin_list,
        allow_credentials=True, allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"])
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.answer_question = answer_question
    app.state.hr_runtime = hr_runtime
    app.state.workbench_runtime = workbench_runtime
    app.state.procurement_runtime = procurement_runtime
    app.state.approval_runtime = approval_runtime
    app.state.rate_limiter = FixedWindowRateLimiter()

    @app.middleware("http")
    async def verify_csrf(request: Request, call_next):  # type: ignore[no-untyped-def]
        if production and request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path != "/api/v1/auth/login" and request.cookies.get(COOKIE_NAME):
            cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
            header_token = request.headers.get("X-CSRF-Token", "")
            if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
                request_id = request.state.request_id
                return JSONResponse(status_code=403, content={"code":"csrf_failed", "message":"CSRF validation failed.", "request_id":request_id},
                    headers={"X-Request-ID":request_id})
        return await call_next(request)

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.attempt_id = str(uuid4())
        try:
            request_id = normalize_request_id(request.headers.get("X-Request-ID"))
        except InvalidRequestId:
            request_id = normalize_request_id(None)
            return JSONResponse(
                status_code=400,
                content={
                    "code": "request_id_invalid",
                    "message": "X-Request-ID is invalid.",
                    "request_id": request_id,
                },
                headers={"X-Request-ID": request_id},
            )
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        route = request.scope.get("route")
        path = getattr(route, "path", "<unmatched>")
        log_event("http_request", request_id=request_id, method=request.method, path=path,
            status_code=response.status_code, duration_ms=round((time.perf_counter()-started)*1000, 2))
        return response

    @app.exception_handler(404)
    async def not_found(request: Request, _exception: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid4()))
        error = ErrorResponse(
            code="not_found",
            message="The requested resource was not found.",
            request_id=request_id,
        )
        return JSONResponse(
            status_code=404,
            content=error.as_dict(),
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(401)
    async def unauthorized(request: Request, _exception: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid4()))
        return JSONResponse(
            status_code=401,
            content={"code": "authentication_required", "message": "Authentication is required.", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(HrPermissionError)
    async def hr_forbidden(request: Request, _exception: HrPermissionError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid4()))
        return JSONResponse(
            status_code=403,
            content={"code": "hr_required", "message": "HR reviewer access is required.", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_failed(
        request: Request, _exception: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid4()))
        return JSONResponse(
            status_code=422,
            content={
                "code": "request_validation_failed",
                "message": "Request validation failed.",
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )
    router = APIRouter(prefix="/api/v1")

    @router.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/ready")
    def readiness(request: Request):
        factory = request.app.state.session_factory
        if factory is None:
            return JSONResponse(status_code=503, content={"code":"not_ready", "message":"The service is not ready.",
                "request_id":request.state.request_id})
        try:
            with factory() as database: database.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse(status_code=503, content={"code":"not_ready", "message":"The service is not ready.",
                "request_id":request.state.request_id})
        return {"status":"ready"}

    if session_factory is not None:
        router.include_router(auth_router)
        router.include_router(documents_router)
        router.include_router(workbench_router)
        if procurement_runtime is not None:
            router.include_router(procurement_router)
        if approval_runtime is not None:
            router.include_router(approvals_router)
        if hr_runtime is not None:
            router.include_router(hr_router)
        if answer_question is not None:
            router.include_router(history_router)
            router.include_router(feedback_router)
    app.include_router(router)
    return app


def create_production_app() -> FastAPI:
    """Build the runnable application from validated environment configuration."""
    settings = Settings()  # type: ignore[call-arg]
    construction_cleanup = ExitStack()

    def construct(factory, closer=None):  # type: ignore[no-untyped-def]
        try:
            resource = factory()
        except BaseException as construction_error:
            try:
                construction_cleanup.close()
            except BaseException as cleanup_error:
                construction_error.add_note(
                    f"Construction cleanup failure: {cleanup_error!r}"
                )
            raise
        if closer is not None:
            construction_cleanup.callback(closer, resource)
        return resource

    engine = construct(
        lambda: create_database_engine(settings.database_url),
        lambda resource: resource.dispose(),
    )
    sessions = construct(lambda: create_session_factory(engine))
    api_key = construct(settings.model_api_key.get_secret_value)
    embedding_client = construct(
        lambda: EmbeddingClient(
            base_url=settings.resolved_embedding_base_url,
            api_key=settings.resolved_embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            timeout=settings.model_timeout_seconds,
            query_prefix=settings.embedding_query_prefix,
            passage_prefix=settings.embedding_passage_prefix,
        ),
        lambda resource: resource.close(),
    )
    answer_client = construct(
        lambda: AnswerModelClient(
            base_url=str(settings.model_base_url),
            api_key=api_key,
            model=settings.chat_model,
            timeout=settings.model_timeout_seconds,
            disable_thinking=settings.model_disable_thinking,
        ),
        lambda resource: resource.close(),
    )
    answer_runtime = construct(
        lambda: AnswerRuntime(
            settings=settings,
            embed_queries=embedding_client.embed_queries,
            generate=answer_client.generate,
        )
    )
    slot_client = construct(
        lambda: SlotExtractionClient(
            base_url=str(settings.model_base_url),
            api_key=api_key,
            model=settings.chat_model,
            timeout=settings.model_timeout_seconds,
        ),
        lambda resource: resource.close(),
    )
    slot_service = construct(
        lambda: SlotExtractionService(
            client=slot_client,
            repository=SlotExtractionOperationRepository(),
            fingerprinter=SlotExtractionFingerprinter(
                settings.session_secret.get_secret_value().encode("utf-8")
            ),
            model_timeout_seconds=settings.model_timeout_seconds,
        )
    )

    def answer_question(question: str) -> AnswerOutcome:
        with sessions() as database:
            outcome, _retrieved = answer_runtime.answer_with_evidence(
                database, question
            )
            return outcome

    def search_policy(database: Session, query: str) -> PolicySearchOutcome:
        outcome, retrieved = answer_runtime.answer_with_evidence(database, query)
        citations = tuple(
            PolicyCitation(
                number=number,
                chunk_id=chunk_id,
                document_name=retrieved[chunk_id].document_name,
                page_number=retrieved[chunk_id].page,
                evidence_snapshot=retrieved[chunk_id].text,
            )
            for number, chunk_id in enumerate(outcome.citations, start=1)
            if chunk_id in retrieved
        )
        refusal_reason = outcome.refusal_reason
        return PolicySearchOutcome(
            status=outcome.status,
            text=outcome.text,
            refusal_reason=(
                refusal_reason.value
                if refusal_reason is not None
                else None
            ),
            citations=citations,
            clarification_questions=outcome.clarification_questions,
        )

    capability_resolver = construct(CapabilityResolver)
    product_event_emitter = construct(ProductEventEmitter)
    procurement_repository = construct(ProcurementRepository)
    procurement_observability = construct(
        lambda: ProcurementObservability(procurement_repository, product_event_emitter)
    )
    approval_engine = construct(ApprovalEngine)
    procurement_service = construct(
        lambda: ProcurementService(
            repository=procurement_repository,
            approval_engine=approval_engine,
            capability_resolver=capability_resolver,
            product_event_emitter=product_event_emitter,
            observability=procurement_observability,
        )
    )
    approval_registry = construct(
        lambda: SubjectAdapterRegistry(
            [ProcurementSubjectAdapter(repository=procurement_repository)]
        )
    )
    approval_access = construct(
        lambda: ProcurementApprovalAccess(
            repository=procurement_repository,
            capability_resolver=capability_resolver,
        )
    )
    approval_core_runtime = construct(
        lambda: ApprovalRuntime(
            registry=approval_registry,
            access=approval_access,
            engine=approval_engine,
            transition_observer=procurement_observability.stage_transition,
            replay_observer=procurement_observability.stage_replayed_transition,
            failure_observer=procurement_observability.stage_attempt,
        )
    )

    approval_runtime = construct(
        lambda: ApprovalApiRuntime(
            approval_core_runtime,
            reader=SqlAlchemyApprovalTaskReader(
                access=approval_access,
                registry=approval_registry,
            ),
        )
    )
    procurement_planner = construct(
        lambda: ToolPlanningClient(
            base_url=str(settings.model_base_url),
            api_key=api_key,
            model=settings.chat_model,
            timeout=settings.model_timeout_seconds,
        ),
        lambda resource: resource.close(),
    )
    procurement_runtime = construct(
        lambda: ProcurementRuntime(
            service=procurement_service,
            capability_resolver=capability_resolver,
            request_reader=SqlAlchemyProcurementRequestReader(
                repository=procurement_repository,
            ),
            planner=procurement_planner,
            slot_extraction=slot_service,
            approval_runtime=approval_runtime,
            search_policy=search_policy,
            max_model_calls=settings.tool_max_model_calls,
            max_read_calls=settings.tool_max_read_calls,
            confirmation_ttl_seconds=settings.tool_confirmation_ttl_seconds,
            observability=procurement_observability,
        )
    )
    hr_planner = construct(
        lambda: ToolPlanningClient(
            base_url=str(settings.model_base_url),
            api_key=api_key,
            model=settings.chat_model,
            timeout=settings.model_timeout_seconds,
        ),
        lambda resource: resource.close(),
    )
    hr_runtime = construct(
        lambda: HrRuntime(
            planner=hr_planner,
            slot_extraction=slot_service,
            search_policy=search_policy,
            max_model_calls=settings.tool_max_model_calls,
            max_read_calls=settings.tool_max_read_calls,
            confirmation_ttl_seconds=settings.tool_confirmation_ttl_seconds,
            capability_resolver=capability_resolver,
            product_event_emitter=product_event_emitter,
        )
    )
    workbench_runtime = construct(
        lambda: WorkbenchRuntime(
            module_catalog=ModuleCatalog(capability_resolver),
            capability_resolver=capability_resolver,
            product_event_emitter=product_event_emitter,
            hr_runtime=hr_runtime,
        )
    )

    app = construct(
        lambda: create_app(
            settings,
            session_factory=sessions,
            answer_question=answer_question,
            hr_runtime=hr_runtime,
            workbench_runtime=workbench_runtime,
            procurement_runtime=procurement_runtime,
            approval_runtime=approval_runtime,
        )
    )
    ingestion_worker = construct(
        lambda: IngestionWorker(IngestionExecutor(
            session_factory=sessions,
            upload_root=settings.upload_root,
            embed=lambda texts: embedding_client.embed_passages(texts),
        )),
        lambda resource: resource.stop(),
    )
    app.state.ingestion_worker = ingestion_worker

    def converge_stale_slot_extractions() -> None:
        with sessions() as database:
            slot_service.repository.converge_stale(
                database,
                now=datetime.now(timezone.utc),
            )
            database.commit()

    app.router.add_event_handler("startup", converge_stale_slot_extractions)
    if settings.app_env == "production":
        construct(lambda: ingestion_worker.start())

    construction_cleanup.pop_all()
    runtime_closed = {
        "worker": False,
        "procurement_planner": False,
        "hr_runtime": False,
        "slot_extraction": False,
        "embedding": False,
        "answer": False,
        "engine": False,
    }

    def close_runtime() -> None:
        def close_hr_runtime() -> None:
            try:
                hr_runtime.close()
            except BaseException:
                hr_runtime._closed = False
                raise

        closers = (
            ("worker", ingestion_worker.stop),
            ("procurement_planner", procurement_planner.close),
            ("hr_runtime", close_hr_runtime),
            ("slot_extraction", slot_client.close),
            ("embedding", embedding_client.close),
            ("answer", answer_client.close),
            ("engine", engine.dispose),
        )
        failures: list[BaseException] = []
        for name, closer in closers:
            if runtime_closed[name]:
                continue
            try:
                closer()
            except BaseException as exception:
                failures.append(exception)
            else:
                runtime_closed[name] = True
        if failures:
            first = failures[0]
            for additional in failures[1:]:
                first.add_note(f"Additional shutdown failure: {additional!r}")
            raise first

    app.state.close_runtime = close_runtime
    app.router.add_event_handler("shutdown", close_runtime)
    return app
