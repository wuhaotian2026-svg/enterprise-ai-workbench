from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session as DatabaseSession

from policy_api.auth.router import current_identity, database_session, error
from policy_api.models import Session, User
from policy_api.workbench.analytics import (
    AnalyticsError,
    AnalyticsWindow,
    resolve_window,
)
from policy_api.workbench.capabilities import (
    Capability,
    OrganizationManagementError,
)
from policy_api.workbench.catalog import DEFAULT_MODULE_KEYS
from policy_api.workbench.events import ProductEventValidationError
from policy_api.workbench.runtime import WorkbenchRuntime
from policy_api.workbench.schemas import (
    AnalyticsResponse,
    CapabilityGrantCreate,
    CapabilityGrantResponse,
    EmployeeAssignmentResponse,
    EmployeeAssignmentUpdate,
    ModuleListResponse,
    ModuleResponse,
    OperationRequest,
    OrganizationUnitCreate,
    OrganizationUnitResponse,
    OrganizationUnitUpdate,
    UiEventRequest,
)


workbench_router = APIRouter(tags=["workbench"])

KNOWN_MODULE_KEYS = DEFAULT_MODULE_KEYS
ANALYTICS_QUERY_KEYS = frozenset({"from", "to", "organization_unit_id"})


def _runtime(request: Request) -> WorkbenchRuntime | None:
    runtime = getattr(request.app.state, "workbench_runtime", None)
    return runtime if isinstance(runtime, WorkbenchRuntime) else None


@workbench_router.get("/workbench/modules", response_model=ModuleListResponse)
def list_modules(
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    runtime = _runtime(request)
    if runtime is None:
        return error(
            request,
            503,
            "workbench_not_ready",
            "The workbench service is not ready.",
        )
    user, _session = identity
    modules = [
        ModuleResponse(key=item.key, label=item.label, index=item.index)
        for item in runtime.module_catalog.allowed_modules(db, user)
        if item.key in KNOWN_MODULE_KEYS
    ]
    return ModuleListResponse(modules=modules)


@workbench_router.post("/analytics/ui-events", status_code=204)
def record_ui_event(
    payload: UiEventRequest,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    runtime = _runtime(request)
    if runtime is None:
        return error(
            request,
            503,
            "workbench_not_ready",
            "The workbench service is not ready.",
        )
    user, _session = identity
    try:
        runtime.record_ui_event(
            db,
            actor=user,
            event_id=payload.event_id,
            event_name=payload.event_name,
            dimensions=payload.dimensions,
            request_id=request.state.request_id,
        )
        db.commit()
    except ProductEventValidationError as exc:
        db.rollback()
        messages = {
            "event_name_not_allowed": "The product event name is not allowed.",
            "event_dimensions_invalid": "The product event dimensions are invalid.",
            "event_id_conflict": "The product event ID conflicts with an earlier event.",
        }
        return error(
            request,
            422,
            exc.code,
            messages.get(exc.code, "The product event is invalid."),
        )
    return Response(status_code=204)


def _analytics_response(
    request: Request,
    db: DatabaseSession,
    user: User,
    *,
    start: datetime | None,
    end: datetime | None,
    organization_unit_id: uuid.UUID | None,
    query: Callable[
        [DatabaseSession, User, AnalyticsWindow, uuid.UUID | None],
        AnalyticsResponse,
    ],
) -> AnalyticsResponse | JSONResponse:
    if any(key not in ANALYTICS_QUERY_KEYS for key in request.query_params):
        return error(
            request,
            422,
            "analytics_range_invalid",
            "The analytics query is invalid.",
        )
    try:
        window = resolve_window(start, end)
        return query(db, user, window, organization_unit_id)
    except AnalyticsError as exc:
        status_code = 403 if exc.code == "analytics_scope_forbidden" else 422
        messages = {
            "analytics_range_invalid": "The analytics time range is invalid.",
            "analytics_scope_forbidden": "The analytics scope is not allowed.",
        }
        return error(
            request,
            status_code,
            exc.code,
            messages.get(exc.code, "The analytics request is invalid."),
        )


def _analytics_runtime_or_error(
    request: Request,
) -> WorkbenchRuntime | JSONResponse:
    runtime = _runtime(request)
    if runtime is None:
        return error(
            request,
            503,
            "workbench_not_ready",
            "The workbench service is not ready.",
        )
    return runtime


@workbench_router.get("/analytics/overview", response_model=AnalyticsResponse)
def analytics_overview(
    request: Request,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    organization_unit_id: uuid.UUID | None = Query(default=None),
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    runtime = _analytics_runtime_or_error(request)
    if isinstance(runtime, JSONResponse):
        return runtime
    user, _session = identity
    return _analytics_response(
        request,
        db,
        user,
        start=start,
        end=end,
        organization_unit_id=organization_unit_id,
        query=runtime.analytics_service.overview,
    )


@workbench_router.get("/analytics/knowledge", response_model=AnalyticsResponse)
def analytics_knowledge(
    request: Request,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    organization_unit_id: uuid.UUID | None = Query(default=None),
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    runtime = _analytics_runtime_or_error(request)
    if isinstance(runtime, JSONResponse):
        return runtime
    user, _session = identity
    return _analytics_response(
        request,
        db,
        user,
        start=start,
        end=end,
        organization_unit_id=organization_unit_id,
        query=runtime.analytics_service.knowledge,
    )


@workbench_router.get("/analytics/hr-funnel", response_model=AnalyticsResponse)
def analytics_hr_funnel(
    request: Request,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    organization_unit_id: uuid.UUID | None = Query(default=None),
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    runtime = _analytics_runtime_or_error(request)
    if isinstance(runtime, JSONResponse):
        return runtime
    user, _session = identity
    return _analytics_response(
        request,
        db,
        user,
        start=start,
        end=end,
        organization_unit_id=organization_unit_id,
        query=runtime.analytics_service.hr_funnel,
    )


@workbench_router.get("/analytics/tools", response_model=AnalyticsResponse)
def analytics_tools(
    request: Request,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    organization_unit_id: uuid.UUID | None = Query(default=None),
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    runtime = _analytics_runtime_or_error(request)
    if isinstance(runtime, JSONResponse):
        return runtime
    user, _session = identity
    return _analytics_response(
        request,
        db,
        user,
        start=start,
        end=end,
        organization_unit_id=organization_unit_id,
        query=runtime.analytics_service.tools,
    )


@workbench_router.get("/analytics/workflows", response_model=AnalyticsResponse)
def analytics_workflows(
    request: Request,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    organization_unit_id: uuid.UUID | None = Query(default=None),
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    runtime = _analytics_runtime_or_error(request)
    if isinstance(runtime, JSONResponse):
        return runtime
    user, _session = identity
    return _analytics_response(
        request,
        db,
        user,
        start=start,
        end=end,
        organization_unit_id=organization_unit_id,
        query=runtime.analytics_service.workflows,
    )


@workbench_router.get("/analytics/procurement-funnel", response_model=AnalyticsResponse)
def analytics_procurement_funnel(
    request: Request,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    organization_unit_id: uuid.UUID | None = Query(default=None),
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    runtime = _analytics_runtime_or_error(request)
    if isinstance(runtime, JSONResponse):
        return runtime
    user, _session = identity
    return _analytics_response(
        request, db, user, start=start, end=end,
        organization_unit_id=organization_unit_id,
        query=runtime.analytics_service.procurement_funnel,
    )


def _authorized_runtime(
    request: Request,
    db: DatabaseSession,
    user: User,
) -> WorkbenchRuntime | JSONResponse:
    runtime = _runtime(request)
    if runtime is None:
        return error(
            request,
            503,
            "workbench_not_ready",
            "The workbench service is not ready.",
        )
    if not runtime.capability_resolver.has(
        db,
        user,
        Capability.ORGANIZATION_MANAGE,
    ):
        return error(
            request,
            403,
            "capability_required",
            "Organization management capability is required.",
        )
    return runtime


def _management_error(
    request: Request,
    exception: OrganizationManagementError,
) -> JSONResponse:
    not_found = {
        "organization_unit_not_found",
        "employee_assignment_invalid",
        "capability_grant_not_found",
    }
    status_code = 404 if exception.code in not_found else 409
    messages = {
        "organization_unit_not_found": "The organization unit was not found.",
        "employee_assignment_invalid": "The employee assignment was not found.",
        "capability_grant_not_found": "The capability grant was not found.",
        "organization_unit_cycle": "The organization hierarchy would contain a cycle.",
        "organization_unit_not_empty": "The organization unit still contains active employees.",
        "manager_assignment_invalid": "The manager assignment is invalid.",
        "capability_grant_conflict": "The capability grant conflicts with existing state.",
        "organization_unit_conflict": "The organization unit conflicts with existing state.",
        "security_audit_operation_conflict": "The operation ID conflicts with an earlier request.",
    }
    return error(
        request,
        status_code,
        exception.code,
        messages.get(exception.code, "The organization operation failed."),
    )


@workbench_router.get(
    "/organization/units",
    response_model=list[OrganizationUnitResponse],
)
def list_organization_units(
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    runtime = _authorized_runtime(request, db, user)
    if isinstance(runtime, JSONResponse):
        return runtime
    return runtime.organization_service.list_units(db)


@workbench_router.post(
    "/organization/units",
    response_model=OrganizationUnitResponse,
    status_code=201,
)
def create_organization_unit(
    payload: OrganizationUnitCreate,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    runtime = _authorized_runtime(request, db, user)
    if isinstance(runtime, JSONResponse):
        return runtime
    try:
        return runtime.organization_service.create_unit(
            db,
            actor=user,
            operation_id=payload.client_operation_id,
            code=payload.code,
            name=payload.name,
            parent_id=payload.parent_id,
            request_id=request.state.request_id,
        )
    except OrganizationManagementError as exc:
        return _management_error(request, exc)


@workbench_router.post(
    "/organization/units/{unit_id}/update",
    response_model=OrganizationUnitResponse,
)
def update_organization_unit(
    unit_id: uuid.UUID,
    payload: OrganizationUnitUpdate,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    runtime = _authorized_runtime(request, db, user)
    if isinstance(runtime, JSONResponse):
        return runtime
    changes = {
        field: getattr(payload, field)
        for field in payload.model_fields_set - {"client_operation_id"}
    }
    try:
        return runtime.organization_service.update_unit(
            db,
            actor=user,
            unit_id=unit_id,
            operation_id=payload.client_operation_id,
            changes=changes,
            request_id=request.state.request_id,
        )
    except OrganizationManagementError as exc:
        return _management_error(request, exc)


@workbench_router.get(
    "/organization/employees",
    response_model=list[EmployeeAssignmentResponse],
)
def list_organization_employees(
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    runtime = _authorized_runtime(request, db, user)
    if isinstance(runtime, JSONResponse):
        return runtime
    return runtime.organization_service.list_employees(db)


@workbench_router.post(
    "/organization/employees/{employee_id}/assignment",
    response_model=EmployeeAssignmentResponse,
)
def update_employee_assignment(
    employee_id: uuid.UUID,
    payload: EmployeeAssignmentUpdate,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    runtime = _authorized_runtime(request, db, user)
    if isinstance(runtime, JSONResponse):
        return runtime
    try:
        return runtime.organization_service.update_employee_assignment(
            db,
            actor=user,
            employee_id=employee_id,
            operation_id=payload.client_operation_id,
            organization_unit_id=payload.organization_unit_id,
            manager_employee_id=payload.manager_employee_id,
            request_id=request.state.request_id,
        )
    except OrganizationManagementError as exc:
        return _management_error(request, exc)


@workbench_router.get(
    "/organization/capability-grants",
    response_model=list[CapabilityGrantResponse],
)
def list_capability_grants(
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    runtime = _authorized_runtime(request, db, user)
    if isinstance(runtime, JSONResponse):
        return runtime
    return runtime.organization_service.list_grants(db)


@workbench_router.post(
    "/organization/capability-grants",
    response_model=CapabilityGrantResponse,
    status_code=201,
)
def create_capability_grant(
    payload: CapabilityGrantCreate,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    runtime = _authorized_runtime(request, db, user)
    if isinstance(runtime, JSONResponse):
        return runtime
    try:
        return runtime.organization_service.create_grant(
            db,
            actor=user,
            operation_id=payload.client_operation_id,
            user_id=payload.user_id,
            capability=payload.capability,
            scope_kind=payload.scope_kind,
            organization_unit_id=payload.organization_unit_id,
            request_id=request.state.request_id,
        )
    except OrganizationManagementError as exc:
        return _management_error(request, exc)


@workbench_router.post(
    "/organization/capability-grants/{grant_id}/revoke",
    response_model=CapabilityGrantResponse,
)
def revoke_capability_grant(
    grant_id: uuid.UUID,
    payload: OperationRequest,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    runtime = _authorized_runtime(request, db, user)
    if isinstance(runtime, JSONResponse):
        return runtime
    try:
        return runtime.organization_service.revoke_grant(
            db,
            actor=user,
            grant_id=grant_id,
            operation_id=payload.client_operation_id,
            request_id=request.state.request_id,
        )
    except OrganizationManagementError as exc:
        return _management_error(request, exc)
