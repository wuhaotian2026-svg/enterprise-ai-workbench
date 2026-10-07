from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from policy_api.hr.enums import LeaveRequestStatus, LeaveTypeCode
from policy_api.hr.schemas import HrDomainError, LeaveRequestView
from policy_api.hr.service import (
    get_leave_duration,
    get_my_leave_balances,
    get_my_leave_request,
    list_my_leave_requests,
)
from policy_api.models import UserRole
from policy_api.tools.authorization import authorize_tool
from policy_api.tools.definitions import ToolContext, ToolDefinition, ToolRisk
from policy_api.tools.errors import ToolError
from policy_api.tools.schemas import ConfirmationBlock


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LeaveBalanceInput(StrictInput):
    year: int | None = Field(default=None, ge=2000, le=2200)


class LeaveDurationInput(StrictInput):
    start_date: date
    end_date: date


class ListLeaveRequestsInput(StrictInput):
    status: LeaveRequestStatus | None = None


class GetLeaveRequestInput(StrictInput):
    request_id: UUID


class SubmitLeaveRequestInput(StrictInput):
    leave_type_code: LeaveTypeCode
    start_date: date
    end_date: date
    reason: str = Field(min_length=1, max_length=2000)


class CancelLeaveRequestInput(StrictInput):
    request_id: UUID


PersistConfirmation = Callable[
    [str, dict[str, object], dict[str, object], ToolContext],
    ConfirmationBlock,
]


def build_hr_tool_definitions(db: Session) -> tuple[ToolDefinition, ...]:
    roles = frozenset({UserRole.EMPLOYEE, UserRole.HR})

    def balances(context: ToolContext, input_data: BaseModel) -> Mapping[str, object]:
        parsed = LeaveBalanceInput.model_validate(input_data.model_dump())
        values = get_my_leave_balances(
            db, context.actor_user_id, year=parsed.year
        )
        serialized = [
            {
                "leave_type_code": item.leave_type_code.value,
                "leave_type_name": item.leave_type_name,
                "year": item.year,
                "entitled": str(item.entitled),
                "used": str(item.used),
                "reserved": str(item.reserved),
                "available": str(item.available),
            }
            for item in values
        ]
        return _facts_result(
            serialized,
            label="leave_balances",
            model_result={"balances": serialized},
        )

    def duration(context: ToolContext, input_data: BaseModel) -> Mapping[str, object]:
        del context
        parsed = LeaveDurationInput.model_validate(input_data.model_dump())
        value = get_leave_duration(db, parsed.start_date, parsed.end_date)
        serialized = {
            "start_date": value.start_date.isoformat(),
            "end_date": value.end_date.isoformat(),
            "workday_count": str(value.workday_count),
        }
        return _facts_result(
            [serialized],
            label="leave_duration",
            model_result=serialized,
        )

    def requests(context: ToolContext, input_data: BaseModel) -> Mapping[str, object]:
        parsed = ListLeaveRequestsInput.model_validate(input_data.model_dump())
        values = list_my_leave_requests(db, context.actor_user_id)
        if parsed.status is not None:
            values = tuple(item for item in values if item.status == parsed.status)
        serialized = [_request_payload(item) for item in values]
        return _facts_result(
            serialized,
            label="leave_requests",
            model_result={"requests": serialized},
        )

    def request(context: ToolContext, input_data: BaseModel) -> Mapping[str, object]:
        parsed = GetLeaveRequestInput.model_validate(input_data.model_dump())
        value = get_my_leave_request(db, context.actor_user_id, parsed.request_id)
        serialized = _request_payload(value)
        return _facts_result(
            [serialized],
            label="leave_request",
            model_result=serialized,
        )

    def write_guard(_context: ToolContext, _input_data: BaseModel) -> Mapping[str, object]:
        raise ToolError("tool_confirmation_required")

    return (
        ToolDefinition(
            name="hr.get_my_leave_balances",
            description=(
                "Get leave balances owned by the current actor, optionally for one year. "
                "This is personal business data, not a policy or eligibility search."
            ),
            input_model=LeaveBalanceInput,
            risk_level=ToolRisk.SENSITIVE_READ,
            allowed_roles=roles,
            requires_confirmation=False,
            timeout_seconds=5,
            result_fields=frozenset({"blocks", "model_result"}),
            handler=balances,
        ),
        ToolDefinition(
            name="hr.calculate_leave_duration",
            description=(
                "Calculate workdays between complete ISO start_date and end_date values "
                "using the enterprise calendar. This does not submit leave."
            ),
            input_model=LeaveDurationInput,
            risk_level=ToolRisk.READ,
            allowed_roles=roles,
            requires_confirmation=False,
            timeout_seconds=5,
            result_fields=frozenset({"blocks", "model_result"}),
            handler=duration,
        ),
        ToolDefinition(
            name="hr.list_my_leave_requests",
            description=(
                "List leave requests owned by the current actor, optionally filtered by "
                "status. Use this before a single-request lookup when no UUID is known."
            ),
            input_model=ListLeaveRequestsInput,
            risk_level=ToolRisk.SENSITIVE_READ,
            allowed_roles=roles,
            requires_confirmation=False,
            timeout_seconds=5,
            result_fields=frozenset({"blocks", "model_result"}),
            handler=requests,
        ),
        ToolDefinition(
            name="hr.get_my_leave_request",
            description=(
                "Get a single leave request owned by the current actor using its UUID. "
                "Do not use this tool to list requests."
            ),
            input_model=GetLeaveRequestInput,
            risk_level=ToolRisk.SENSITIVE_READ,
            allowed_roles=roles,
            requires_confirmation=False,
            timeout_seconds=5,
            result_fields=frozenset({"blocks", "model_result"}),
            handler=request,
        ),
        ToolDefinition(
            name="hr.submit_leave_request",
            description=(
                "Create only a leave-request proposal for explicit user confirmation; it "
                "does not submit the request. Supply all four required fields: "
                "leave_type_code (annual or compensatory), start_date, end_date, and reason. "
                "When the user explicitly requested leave and all four fields are complete, "
                "call this proposal tool now instead of asking for a separate confirmation; "
                "the proposal result is the confirmation step."
            ),
            input_model=SubmitLeaveRequestInput,
            risk_level=ToolRisk.WRITE,
            allowed_roles=roles,
            requires_confirmation=True,
            timeout_seconds=5,
            result_fields=frozenset(),
            handler=write_guard,
        ),
        ToolDefinition(
            name="hr.cancel_leave_request",
            description=(
                "Create only a cancellation proposal for a pending leave request owned by "
                "the current actor. Supply its UUID; approved, rejected, or cancelled "
                "requests cannot be cancelled."
            ),
            input_model=CancelLeaveRequestInput,
            risk_level=ToolRisk.WRITE,
            allowed_roles=roles,
            requires_confirmation=True,
            timeout_seconds=5,
            result_fields=frozenset(),
            handler=write_guard,
        ),
    )


def propose_hr_write(
    db: Session,
    definition: ToolDefinition,
    arguments: Mapping[str, object],
    context: ToolContext,
    *,
    persist: PersistConfirmation,
) -> ConfirmationBlock:
    authorize_tool(definition, context)
    try:
        parsed = definition.input_model.model_validate(dict(arguments))
        if definition.name == "hr.submit_leave_request":
            submit = SubmitLeaveRequestInput.model_validate(parsed.model_dump())
            duration = get_leave_duration(db, submit.start_date, submit.end_date)
            balances = get_my_leave_balances(
                db, context.actor_user_id, year=submit.start_date.year
            )
            balance = next(
                (
                    item
                    for item in balances
                    if item.leave_type_code == submit.leave_type_code
                ),
                None,
            )
            if balance is None:
                raise HrDomainError("leave_account_not_found")
            if balance.available < duration.workday_count:
                raise HrDomainError("leave_balance_insufficient")
            for request in list_my_leave_requests(db, context.actor_user_id):
                if (
                    request.status
                    in {LeaveRequestStatus.PENDING, LeaveRequestStatus.APPROVED}
                    and request.start_date <= submit.end_date
                    and request.end_date >= submit.start_date
                ):
                    raise HrDomainError("leave_request_overlap")
            normalized = _json_payload(submit.model_dump())
            preview = {
                **normalized,
                "workday_count": str(duration.workday_count),
                "available_before": str(balance.available),
                "available_after": str(balance.available - duration.workday_count),
            }
            return persist(definition.name, normalized, preview, context)
        if definition.name == "hr.cancel_leave_request":
            cancel = CancelLeaveRequestInput.model_validate(parsed.model_dump())
            request = get_my_leave_request(
                db, context.actor_user_id, cancel.request_id
            )
            if request.status != LeaveRequestStatus.PENDING:
                raise HrDomainError("leave_request_state_conflict")
            normalized = _json_payload(cancel.model_dump())
            preview = {
                "request_id": str(request.id),
                "request_number": request.request_number,
                "status": request.status.value,
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
                "workday_count": str(request.workday_count),
            }
            return persist(definition.name, normalized, preview, context)
        raise ToolError("unknown_tool")
    except ValidationError:
        raise ToolError("invalid_tool_arguments") from None
    except HrDomainError as exc:
        raise ToolError(exc.code) from None


def _facts_result(
    values: list[object],
    *,
    label: str,
    model_result: Mapping[str, object],
) -> dict[str, object]:
    queried_at = datetime.now(timezone.utc)
    return {
        "blocks": [
            {
                "type": "business_facts",
                "facts": [{"label": label, "value": values}],
                "queried_at": queried_at.isoformat(),
            }
        ],
        "model_result": dict(model_result),
    }


def _request_payload(item: LeaveRequestView) -> dict[str, object]:
    return {
        "id": str(item.id),
        "request_number": item.request_number,
        "leave_type_code": item.leave_type_code.value,
        "leave_type_name": item.leave_type_name,
        "start_date": item.start_date.isoformat(),
        "end_date": item.end_date.isoformat(),
        "workday_count": str(item.workday_count),
        "status": item.status.value,
        "submitted_at": item.submitted_at.isoformat(),
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "rejection_reason": item.rejection_reason,
        "cancelled_at": item.cancelled_at.isoformat() if item.cancelled_at else None,
    }


def _json_payload(values: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values.items():
        if isinstance(value, (date, datetime)):
            result[key] = value.isoformat()
        elif isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, Enum):
            result[key] = value.value
        else:
            result[key] = value
    return result
