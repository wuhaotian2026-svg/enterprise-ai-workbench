from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session as DatabaseSession

from policy_api.auth.router import current_identity, database_session, error
from policy_api.hr.permissions import require_hr
from policy_api.hr.schemas import (
    ConversationCreate,
    EmptyRequest,
    HrDomainError,
    OperationRequest,
    RejectRequest,
    TurnCreate,
)
from policy_api.models import Session, User
from policy_api.rate_limit import limited_response
from policy_api.tools.errors import ToolError


router = APIRouter(prefix="/hr", tags=["hr"])


class HrApiError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(code)


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "hr_runtime", None)
    if runtime is None:
        raise HrApiError(503, "hr_not_ready", "The HR service is not ready.", retryable=True)
    return runtime


def _invoke(request: Request, operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except HrApiError as exc:
        payload: dict[str, object] = {
            "code": exc.code,
            "message": exc.message,
            "request_id": request.state.request_id,
        }
        if exc.retryable:
            payload["retryable"] = True
        return JSONResponse(status_code=exc.status_code, content=payload)
    except ToolError as exc:
        status = 404 if exc.code.endswith("_not_found") else 409
        return error(request, status, exc.code, "The HR operation could not be completed.")
    except HrDomainError as exc:
        if exc.code.endswith("_not_found"):
            status = 404
        elif exc.code == "capability_required":
            status = 403
        elif exc.code.endswith("_conflict") or exc.code in {
            "operation_id_conflict",
            "client_turn_id_conflict",
            "confirmation_already_used",
            "confirmation_cancelled",
        }:
            status = 409
        else:
            status = 422
        return error(
            request,
            status,
            exc.code,
            "The HR operation could not be completed.",
        )


def _rate_limit(request: Request, key: str, limit: int) -> JSONResponse | None:
    retry_after = request.app.state.rate_limiter.check(key, limit)
    if retry_after is not None:
        return limited_response(request, retry_after)
    return None


@router.post("/conversations", response_model=None)
def create_conversation(
    payload: ConversationCreate,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    return _invoke(
        request,
        lambda: _runtime(request).create_conversation(db, user.id, payload.title),
    )


@router.get("/conversations", response_model=None)
def list_conversations(
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    return _invoke(request, lambda: _runtime(request).list_conversations(db, user.id))


@router.get("/conversations/{conversation_id}", response_model=None)
def get_conversation(
    conversation_id: UUID,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    return _invoke(
        request,
        lambda: _runtime(request).get_conversation(db, user.id, conversation_id),
    )


@router.post("/conversations/{conversation_id}/archive", status_code=204)
def archive_conversation(
    conversation_id: UUID,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity

    def archive() -> Response:
        _runtime(request).archive_conversation(db, user.id, conversation_id)
        return Response(status_code=204)

    return _invoke(request, archive)


@router.post("/conversations/{conversation_id}/turns", response_model=None)
def run_turn(
    conversation_id: UUID,
    payload: TurnCreate,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    limited = _rate_limit(
        request,
        f"hr-turn:{user.id}",
        request.app.state.settings.hr_turn_rate_limit_per_minute,
    )
    if limited is not None:
        return limited
    return _invoke(
        request,
        lambda: _runtime(request).run_turn(
            db,
            user.id,
            conversation_id,
            payload.client_turn_id,
            payload.text,
            request_id=request.state.request_id,
        ),
    )


@router.post("/confirmations/{confirmation_id}/confirm", response_model=None)
def confirm(
    confirmation_id: UUID,
    payload: OperationRequest,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    limited = _rate_limit(
        request,
        f"hr-confirmation:{user.id}",
        request.app.state.settings.hr_confirmation_rate_limit_per_minute,
    )
    if limited is not None:
        return limited
    return _invoke(
        request,
        lambda: _runtime(request).confirm(
            db,
            user.id,
            confirmation_id,
            payload.client_operation_id,
            trace_id=request.state.request_id,
        ),
    )


@router.post("/confirmations/{confirmation_id}/cancel", response_model=None)
def cancel_confirmation(
    confirmation_id: UUID,
    _payload: EmptyRequest,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    limited = _rate_limit(
        request,
        f"hr-confirmation:{user.id}",
        request.app.state.settings.hr_confirmation_rate_limit_per_minute,
    )
    if limited is not None:
        return limited
    return _invoke(
        request,
        lambda: _runtime(request).cancel_confirmation(
            db,
            user.id,
            confirmation_id,
            trace_id=request.state.request_id,
        ),
    )


@router.get("/leave-balances", response_model=None)
def leave_balances(
    request: Request,
    year: int | None = Query(default=None, ge=2000, le=2200),
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    return _invoke(request, lambda: _runtime(request).leave_balances(db, user.id, year))


@router.get("/leave-requests", response_model=None)
def leave_requests(
    request: Request,
    status: Literal["pending", "approved", "rejected", "cancelled"] | None = None,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    return _invoke(
        request, lambda: _runtime(request).list_leave_requests(db, user.id, status)
    )


@router.get("/leave-requests/{request_id}", response_model=None)
def leave_request(
    request_id: UUID,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    return _invoke(
        request,
        lambda: _runtime(request).get_leave_request(db, user.id, request_id),
    )


@router.post("/leave-requests/{request_id}/cancel-intent", response_model=None)
def cancel_intent(
    request_id: UUID,
    payload: OperationRequest,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    limited = _rate_limit(
        request,
        f"hr-confirmation:{user.id}",
        request.app.state.settings.hr_confirmation_rate_limit_per_minute,
    )
    if limited is not None:
        return limited
    return _invoke(
        request,
        lambda: _runtime(request).cancel_intent(
            db,
            user.id,
            request_id,
            payload.client_operation_id,
            trace_id=request.state.request_id,
        ),
    )


@router.get("/review-queue", response_model=None)
def review_queue(
    request: Request,
    status: Literal["pending", "approved", "rejected", "cancelled"] = "pending",
    identity: tuple[User, Session] = Depends(require_hr),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    return _invoke(
        request,
        lambda: _runtime(request).review_queue(db, user.id, status),
    )


@router.get("/review-queue/{request_id}", response_model=None)
def review_detail(
    request_id: UUID,
    request: Request,
    identity: tuple[User, Session] = Depends(require_hr),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    return _invoke(
        request,
        lambda: _runtime(request).review_detail(db, user.id, request_id),
    )


@router.post("/review-queue/{request_id}/approve", response_model=None)
def approve(
    request_id: UUID,
    payload: OperationRequest,
    request: Request,
    identity: tuple[User, Session] = Depends(require_hr),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    return _invoke(
        request,
        lambda: _runtime(request).approve(
            db,
            user.id,
            request_id,
            payload.client_operation_id,
            trace_id=request.state.request_id,
        ),
    )


@router.post("/review-queue/{request_id}/reject", response_model=None)
def reject(
    request_id: UUID,
    payload: RejectRequest,
    request: Request,
    identity: tuple[User, Session] = Depends(require_hr),
    db: DatabaseSession = Depends(database_session),
):
    user, _session = identity
    return _invoke(
        request,
        lambda: _runtime(request).reject(
            db,
            user.id,
            request_id,
            payload.client_operation_id,
            payload.reason,
            trace_id=request.state.request_id,
        ),
    )
