"""Capability-scoped approval task REST routes."""

from __future__ import annotations

import inspect

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session as DatabaseSession

from policy_api.approvals.schemas import ApprovalTaskDetail, ApprovalTaskSummary
from policy_api.auth.router import current_identity, database_session, error
from policy_api.models import Session, User
from policy_api.rate_limit import limited_response
from policy_api.tools.errors import ToolError


router = APIRouter(prefix="/approvals", tags=["approvals"])


class ClosedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApproveRequest(ClosedBody):
    client_operation_id: UUID
    comment: str | None = Field(default=None, max_length=500)


class RejectRequest(ClosedBody):
    client_operation_id: UUID
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def reject_blank_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


class ClosedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovalTaskPageResponse(ClosedResponse):
    items: tuple[ApprovalTaskSummary, ...]
    offset: int
    limit: int
    total: int


class ApprovalTransitionResponse(ClosedResponse):
    instance_id: UUID
    status: str
    current_step_key: str | None
    replayed: bool


def _runtime(request: Request):
    return getattr(request.app.state, "approval_runtime", None)


def _request_context(method, request_id: str) -> dict[str, str]:
    parameters = inspect.signature(method).parameters
    for name in ("server_attempt_id", "request_id"):
        if name in parameters:
            return {name: request_id}
    return {}


def _call_with_request_id(method, args: tuple, kwargs: dict, request_id: str):
    context = _request_context(method, request_id)
    return method(*args, **kwargs, **context)


def _invoke(request: Request, operation):
    try:
        return operation()
    except ToolError as exc:
        if exc.code.endswith("_not_found") or exc.code == "approval_task_not_assigned":
            status = 404
        elif exc.code in {"approval_capability_required", "approval_scope_denied"}:
            status = 403
        elif exc.code.endswith("_conflict"):
            status = 409
        else:
            status = 422
        return error(
            request,
            status,
            exc.code,
            "The approval operation could not be completed.",
        )


def _write_limit(request: Request, actor: User) -> JSONResponse | None:
    retry_after = request.app.state.rate_limiter.check(
        f"approval-command:{actor.id}",
        request.app.state.settings.hr_confirmation_rate_limit_per_minute,
    )
    return limited_response(request, retry_after) if retry_after is not None else None


def _not_ready(request: Request) -> JSONResponse:
    return error(
        request,
        503,
        "approval_not_ready",
        "The approval service is not ready.",
    )


def _closed_response(request: Request, model_type, value):
    try:
        return model_type.model_validate(value, extra="ignore")
    except ValidationError:
        return error(
            request,
            500,
            "api_response_invalid",
            "The service produced an invalid response.",
        )


def _utc_boundary(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _closed_query(
    request: Request, allowed: frozenset[str]
) -> JSONResponse | None:
    if set(request.query_params).issubset(allowed):
        return None
    return error(
        request,
        422,
        "request_validation_failed",
        "Request validation failed.",
    )


@router.get("/tasks", response_model=ApprovalTaskPageResponse)
def list_tasks(
    request: Request,
    status: Literal[
        "waiting", "pending", "approved", "rejected", "cancelled"
    ] | None = "pending",
    process_key: Literal["procurement.request"] | None = None,
    activated_from: datetime | None = None,
    activated_to: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    actor, _session = identity
    invalid_query = _closed_query(
        request,
        frozenset(
            {
                "status",
                "process_key",
                "activated_from",
                "activated_to",
                "offset",
                "limit",
            }
        ),
    )
    if invalid_query is not None:
        return invalid_query
    normalized_from = _utc_boundary(activated_from)
    normalized_to = _utc_boundary(activated_to)
    if (
        (activated_from is not None and normalized_from is None)
        or (activated_to is not None and normalized_to is None)
    ):
        return error(request, 422, "request_validation_failed", "Request validation failed.")
    if (
        normalized_from is not None
        and normalized_to is not None
        and normalized_from > normalized_to
    ):
        return error(request, 422, "request_validation_failed", "Request validation failed.")
    runtime = _runtime(request)
    if runtime is None:
        return _not_ready(request)
    result = _invoke(
        request,
        lambda: runtime.list_tasks(
            db,
            actor=actor,
            status=status,
            process_key=process_key,
            activated_from=normalized_from,
            activated_to=normalized_to,
            offset=offset,
            limit=limit,
        ),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(request, ApprovalTaskPageResponse, result)


@router.get("/tasks/{task_id}", response_model=ApprovalTaskDetail)
def get_task(
    task_id: UUID,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    actor, _session = identity
    runtime = _runtime(request)
    if runtime is None:
        return _not_ready(request)
    result = _invoke(
        request,
        lambda: runtime.get_task(db, actor=actor, task_id=task_id),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(request, ApprovalTaskDetail, result)


@router.post("/tasks/{task_id}/approve", response_model=ApprovalTransitionResponse)
def approve_task(
    task_id: UUID,
    payload: ApproveRequest,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    actor, _session = identity
    limited = _write_limit(request, actor)
    if limited is not None:
        return limited
    runtime = _runtime(request)
    if runtime is None:
        return _not_ready(request)
    result = _invoke(
        request,
        lambda: _call_with_request_id(runtime.approve, (db,), {
            "actor": actor, "task_id": task_id,
            "client_operation_id": payload.client_operation_id,
            "comment": payload.comment,
        }, request.state.attempt_id),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(request, ApprovalTransitionResponse, result)


@router.post("/tasks/{task_id}/reject", response_model=ApprovalTransitionResponse)
def reject_task(
    task_id: UUID,
    payload: RejectRequest,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    actor, _session = identity
    limited = _write_limit(request, actor)
    if limited is not None:
        return limited
    runtime = _runtime(request)
    if runtime is None:
        return _not_ready(request)
    result = _invoke(
        request,
        lambda: _call_with_request_id(runtime.reject, (db,), {
            "actor": actor, "task_id": task_id,
            "client_operation_id": payload.client_operation_id,
            "reason": payload.reason,
        }, request.state.attempt_id),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(request, ApprovalTransitionResponse, result)


__all__ = ["router"]
