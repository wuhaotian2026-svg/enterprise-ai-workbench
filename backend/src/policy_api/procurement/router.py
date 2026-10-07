"""Cookie-authenticated deterministic procurement REST routes."""

from __future__ import annotations

import inspect
import re

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    field_validator,
)
from sqlalchemy.orm import Session as DatabaseSession

from policy_api.auth.router import current_identity, database_session, error
from policy_api.approvals.schemas import (
    SubjectApplicant,
    SubjectItem,
    SubjectOrganization,
    SubjectSummary,
    SubjectTimelineEntry,
)
from policy_api.models import Session, User
from policy_api.procurement.calculation import calculate_total
from policy_api.procurement.schemas import ProcurementRequestInput
from policy_api.rate_limit import limited_response
from policy_api.tools.errors import ToolError


router = APIRouter(prefix="/procurement", tags=["procurement"])
_CANONICAL_AMOUNT_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.[0-9]{2}")


def _canonical_amount(value: object, *, maximum_integer_digits: int) -> str:
    if not isinstance(value, str):
        raise ValueError("amount_must_be_canonical_string")
    match = _CANONICAL_AMOUNT_PATTERN.fullmatch(value)
    if match is None or len(match.group(1)) > maximum_integer_digits:
        raise ValueError("amount_must_be_canonical_string")
    return value


class SubmitRequest(ProcurementRequestInput):
    client_operation_id: UUID


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_operation_id: UUID


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=160)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ConversationTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_turn_id: UUID
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("turn_text_required")
        return value


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClosedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfirmationExecutionResponse(ClosedResponse):
    type: Literal["execution_result"]
    resource_type: Literal["procurement_request"]
    resource_id: UUID
    replayed: bool


class ConfirmationCancelResponse(ClosedResponse):
    confirmation_id: UUID
    status: Literal["cancelled"]


class ConversationSummaryResponse(ClosedResponse):
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationTurnSummaryResponse(ClosedResponse):
    id: UUID
    client_turn_id: UUID
    role: Literal["user", "assistant"]
    request_content: str
    text: str
    blocks: tuple[dict[str, object], ...]
    created_at: datetime


class ConversationTurnResponse(ConversationTurnSummaryResponse):
    replayed: bool


class ConversationDetailResponse(ConversationSummaryResponse):
    turns: tuple[ConversationTurnSummaryResponse, ...]


class ConversationListResponse(RootModel[tuple[ConversationSummaryResponse, ...]]):
    pass


class ProcurementListItemResponse(ClosedResponse):
    id: UUID
    request_number: str
    title: str
    total: Decimal
    status: str
    submitted_at: datetime


class ProcurementSubmitResponse(ProcurementListItemResponse):
    replayed: bool


class ProcurementPreviewResponse(ClosedResponse):
    currency: Literal["CNY"]
    subtotals: tuple[str, ...] = Field(min_length=1, max_length=50)
    total: str

    @field_validator("subtotals", mode="before")
    @classmethod
    def validate_subtotals(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            raise ValueError("subtotals_must_be_array")
        return tuple(
            _canonical_amount(item, maximum_integer_digits=12)
            for item in value
        )

    @field_validator("total", mode="before")
    @classmethod
    def validate_total(cls, value: object) -> str:
        return _canonical_amount(value, maximum_integer_digits=12)


class ProcurementPageResponse(ClosedResponse):
    items: tuple[ProcurementListItemResponse, ...]
    offset: int
    limit: int
    total: int


class ProcurementDetailResponse(ClosedResponse):
    id: UUID
    summary: SubjectSummary
    purpose: str
    needed_by_date: date
    currency: str
    items: tuple[SubjectItem, ...]
    applicant: SubjectApplicant
    organization: SubjectOrganization
    timeline: tuple[SubjectTimelineEntry, ...]


class CommandTransitionResponse(ClosedResponse):
    instance_id: UUID
    status: str
    current_step_key: str | None
    replayed: bool


def _runtime(request: Request):
    return getattr(request.app.state, "procurement_runtime", None)


def _request_context(
    method, request_id: str, occupied: frozenset[str] = frozenset()
) -> dict[str, str]:
    parameters = inspect.signature(method).parameters
    for name in ("server_attempt_id", "request_id", "request_trace_id"):
        if name in parameters and name not in occupied:
            return {name: request_id}
    return (
        {"request_id": request_id}
        if any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        )
        and "request_id" not in occupied
        else {}
    )


def _call_with_request_id(method, args: tuple, kwargs: dict, request_id: str):
    context = _request_context(method, request_id, frozenset(kwargs))
    return method(*args, **kwargs, **context)


def _invoke(request: Request, operation):
    try:
        return operation()
    except ToolError as exc:
        if exc.code.endswith("_not_found"):
            status = 404
        elif exc.code in {
            "procurement_profile_required",
            "approval_capability_required",
            "approval_scope_denied",
        }:
            status = 403
        elif exc.code.endswith("_conflict") or exc.code == "operation_id_conflict":
            status = 409
        else:
            status = 422
        return error(
            request,
            status,
            exc.code,
            "The procurement operation could not be completed.",
        )


def _write_limit(request: Request, actor: User) -> JSONResponse | None:
    retry_after = request.app.state.rate_limiter.check(
        f"procurement-command:{actor.id}",
        request.app.state.settings.hr_confirmation_rate_limit_per_minute,
    )
    return limited_response(request, retry_after) if retry_after is not None else None


def _not_ready(request: Request) -> JSONResponse:
    return error(
        request,
        503,
        "procurement_not_ready",
        "The procurement service is not ready.",
    )


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


def _strict_preview_response(request: Request, value):
    try:
        return ProcurementPreviewResponse.model_validate(value)
    except ValidationError:
        return error(
            request,
            500,
            "api_response_invalid",
            "The service produced an invalid response.",
        )


@router.post("/requests/preview", response_model=ProcurementPreviewResponse)
def preview_request(
    payload: ProcurementRequestInput,
    request: Request,
    _identity: tuple[User, Session] = Depends(current_identity),
):
    runtime = _runtime(request)
    if runtime is None:
        return _not_ready(request)
    result = _invoke(request, lambda: runtime.preview_request(payload))
    if isinstance(result, JSONResponse):
        return result
    public_result = _strict_preview_response(request, result)
    if isinstance(public_result, JSONResponse):
        return public_result
    if len(public_result.subtotals) != len(payload.items):
        return error(
            request,
            500,
            "api_response_invalid",
            "The service produced an invalid response.",
        )
    try:
        expected = calculate_total(payload.items)
    except ValueError:
        return error(
            request,
            500,
            "api_response_invalid",
            "The service produced an invalid response.",
        )
    expected_subtotals = tuple(format(value, ".2f") for value in expected.subtotals)
    expected_total = format(expected.total_amount, ".2f")
    if (
        public_result.subtotals != expected_subtotals
        or public_result.total != expected_total
    ):
        return error(
            request,
            500,
            "api_response_invalid",
            "The service produced an invalid response.",
        )
    return public_result


@router.post("/requests", response_model=ProcurementSubmitResponse)
def submit_request(
    payload: SubmitRequest,
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
    request_input = ProcurementRequestInput.model_validate(
        payload.model_dump(exclude={"client_operation_id"})
    )
    result = _invoke(
        request,
        lambda: _call_with_request_id(
            runtime.submit_request,
            (db,),
            {
                "actor": actor,
                "client_operation_id": payload.client_operation_id,
                "request_input": request_input,
            },
            request.state.attempt_id,
        ),
    )
    if isinstance(result, JSONResponse):
        return result
    public_result = _closed_response(request, ProcurementSubmitResponse, result)
    if isinstance(public_result, JSONResponse):
        return public_result
    return JSONResponse(
        status_code=200 if public_result.replayed else 201,
        content=jsonable_encoder(public_result.model_dump(mode="json")),
    )


@router.post("/conversations", response_model=None)
def create_conversation(
    payload: ConversationCreate,
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
        lambda: runtime.create_conversation(db, actor.id, payload.title),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(
        request, ConversationSummaryResponse, result
    )


@router.get("/conversations", response_model=None)
def list_conversations(
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    actor, _session = identity
    runtime = _runtime(request)
    if runtime is None:
        return _not_ready(request)
    result = _invoke(request, lambda: runtime.list_conversations(db, actor.id))
    return result if isinstance(result, JSONResponse) else _closed_response(
        request, ConversationListResponse, result
    )


@router.get("/conversations/{conversation_id}", response_model=None)
def get_conversation(
    conversation_id: UUID,
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
        lambda: runtime.get_conversation(db, actor.id, conversation_id),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(
        request, ConversationDetailResponse, result
    )


@router.post("/conversations/{conversation_id}/turns", response_model=None)
def run_turn(
    conversation_id: UUID,
    payload: ConversationTurnRequest,
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
        lambda: _call_with_request_id(
            runtime.run_turn,
            (
                db,
                actor.id,
                conversation_id,
                payload.client_turn_id,
                payload.text,
            ),
            {},
            request.state.attempt_id,
        ),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(
        request, ConversationTurnResponse, result
    )


@router.post(
    "/confirmations/{confirmation_id}/confirm",
    response_model=ConfirmationExecutionResponse,
)
def confirm_submission(
    confirmation_id: UUID,
    payload: OperationRequest,
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
        lambda: _call_with_request_id(
            runtime.confirm_submission,
            (db,),
            {
                "actor": actor,
                "confirmation_id": confirmation_id,
                "client_operation_id": payload.client_operation_id,
            },
            request.state.attempt_id,
        ),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(
        request, ConfirmationExecutionResponse, result
    )


@router.post(
    "/confirmations/{confirmation_id}/cancel",
    response_model=ConfirmationCancelResponse,
)
def cancel_confirmation(
    confirmation_id: UUID,
    _payload: EmptyRequest,
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
        lambda: _call_with_request_id(
            runtime.cancel_confirmation,
            (db, actor.id, confirmation_id),
            {},
            request.state.attempt_id,
        ),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(
        request, ConfirmationCancelResponse, result
    )


@router.get("/requests", response_model=ProcurementPageResponse)
def list_requests(
    request: Request,
    status: Literal[
        "pending_manager",
        "pending_procurement",
        "approved",
        "rejected",
        "cancelled",
    ] | None = None,
    submitted_from: date | None = None,
    submitted_to: date | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
):
    actor, _session = identity
    invalid_query = _closed_query(
        request,
        frozenset({"status", "submitted_from", "submitted_to", "offset", "limit"}),
    )
    if invalid_query is not None:
        return invalid_query
    if (
        submitted_from is not None
        and submitted_to is not None
        and submitted_from > submitted_to
    ):
        return error(request, 422, "request_validation_failed", "Request validation failed.")
    runtime = _runtime(request)
    if runtime is None:
        return _not_ready(request)
    result = _invoke(
        request,
        lambda: runtime.list_requests(
            db,
            actor=actor,
            status=status,
            submitted_from=submitted_from,
            submitted_to=submitted_to,
            offset=offset,
            limit=limit,
        ),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(request, ProcurementPageResponse, result)


@router.get("/requests/{request_id}", response_model=ProcurementDetailResponse)
def get_request(
    request_id: UUID,
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
        lambda: runtime.get_request(db, actor=actor, request_id=request_id),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(request, ProcurementDetailResponse, result)


@router.post("/requests/{request_id}/withdraw", response_model=CommandTransitionResponse)
def withdraw_request(
    request_id: UUID,
    payload: OperationRequest,
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
        lambda: _call_with_request_id(runtime.withdraw_request, (db,), {
            "actor": actor, "request_id": request_id,
            "client_operation_id": payload.client_operation_id,
        }, request.state.attempt_id),
    )
    return result if isinstance(result, JSONResponse) else _closed_response(request, CommandTransitionResponse, result)


__all__ = ["router"]
