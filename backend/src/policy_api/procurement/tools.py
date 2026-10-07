from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from policy_api.models import User, UserRole
from policy_api.procurement.calculation import calculate_total
from policy_api.procurement.schemas import ProcurementRequestInput
from policy_api.tools.authorization import authorize_tool
from policy_api.tools.definitions import ToolContext, ToolDefinition, ToolRisk
from policy_api.tools.errors import ToolError
from policy_api.tools.schemas import ConfirmationBlock


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListMyRequestsInput(StrictInput):
    status: str | None = None
    submitted_from: date | None = None
    submitted_to: date | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class GetMyRequestInput(StrictInput):
    request_id: UUID


class CalculationItemInput(StrictInput):
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    estimated_unit_price: Decimal = Field(ge=0, allow_inf_nan=False)


class CalculateRequestTotalInput(StrictInput):
    items: list[CalculationItemInput] = Field(min_length=1, max_length=50)


class SubmitRequestInput(ProcurementRequestInput):
    pass


class WithdrawRequestInput(StrictInput):
    request_id: UUID


class ListMyPendingTasksInput(StrictInput):
    pass


class GetTaskDetailInput(StrictInput):
    task_id: UUID


class ApproveTaskInput(StrictInput):
    task_id: UUID
    comment: str | None = Field(default=None, max_length=2000)


class RejectTaskInput(StrictInput):
    task_id: UUID
    reason: str = Field(min_length=1, max_length=2000)


class ProcurementRuntimePort(Protocol):
    def list_requests(self, db: Session, **kwargs: object) -> dict[str, object]: ...
    def get_request(self, db: Session, **kwargs: object) -> dict[str, object]: ...
    def preflight_submit(self, db: Session, **kwargs: object) -> object: ...


class ApprovalRuntimePort(Protocol):
    def list_tasks(self, db: Session, **kwargs: object) -> dict[str, object]: ...
    def get_task(self, db: Session, **kwargs: object) -> dict[str, object]: ...


PersistConfirmation = Callable[
    [str, dict[str, object], dict[str, object], ToolContext],
    ConfirmationBlock,
]


def build_procurement_tool_definitions(
    db: Session,
    *,
    procurement_runtime: ProcurementRuntimePort,
    approval_runtime: ApprovalRuntimePort,
) -> tuple[ToolDefinition, ...]:
    roles = frozenset({UserRole.EMPLOYEE, UserRole.HR, UserRole.ADMIN})

    def actor(context: ToolContext) -> User:
        value = db.get(User, context.actor_user_id)
        if value is None or not value.is_active:
            raise ToolError("authentication_required")
        return value

    def list_requests(
        context: ToolContext, input_data: BaseModel
    ) -> Mapping[str, object]:
        parsed = ListMyRequestsInput.model_validate(input_data.model_dump())
        value = procurement_runtime.list_requests(
            db,
            actor=actor(context),
            status=parsed.status,
            submitted_from=parsed.submitted_from,
            submitted_to=parsed.submitted_to,
            offset=parsed.offset,
            limit=parsed.limit,
        )
        return _facts_result("procurement_requests", value.get("items", []), value)

    def get_request(
        context: ToolContext, input_data: BaseModel
    ) -> Mapping[str, object]:
        parsed = GetMyRequestInput.model_validate(input_data.model_dump())
        value = procurement_runtime.get_request(
            db, actor=actor(context), request_id=parsed.request_id
        )
        projected = _request_projection(value)
        return _facts_result("procurement_request", [projected], projected)

    def calculate(
        _context: ToolContext, input_data: BaseModel
    ) -> Mapping[str, object]:
        parsed = CalculateRequestTotalInput.model_validate(input_data.model_dump())
        calculated = calculate_total(parsed.items)
        value = {
            "subtotals": [str(item) for item in calculated.subtotals],
            "total": str(calculated.total_amount),
        }
        return _facts_result("procurement_total", [value], value)

    def list_tasks(
        context: ToolContext, input_data: BaseModel
    ) -> Mapping[str, object]:
        ListMyPendingTasksInput.model_validate(input_data.model_dump())
        value = approval_runtime.list_tasks(
            db,
            actor=actor(context),
            status="pending",
            process_key="procurement_request_v1",
            activated_from=None,
            activated_to=None,
            offset=0,
            limit=100,
        )
        return _facts_result("approval_tasks", value.get("items", []), value)

    def task_detail(
        context: ToolContext, input_data: BaseModel
    ) -> Mapping[str, object]:
        parsed = GetTaskDetailInput.model_validate(input_data.model_dump())
        value = approval_runtime.get_task(
            db, actor=actor(context), task_id=parsed.task_id
        )
        result = _task_projection(value)
        return _facts_result("approval_task", [result], result)

    def write_guard(
        _context: ToolContext, _input_data: BaseModel
    ) -> Mapping[str, object]:
        raise ToolError("tool_confirmation_required")

    def definition(
        name: str,
        description: str,
        input_model: type[BaseModel],
        risk: ToolRisk,
        handler,
    ) -> ToolDefinition:  # type: ignore[no-untyped-def]
        return ToolDefinition(
            name=name,
            description=description,
            input_model=input_model,
            risk_level=risk,
            allowed_roles=roles,
            requires_confirmation=risk is ToolRisk.WRITE,
            timeout_seconds=5,
            result_fields=(
                frozenset() if risk is ToolRisk.WRITE
                else frozenset({"blocks", "model_result"})
            ),
            handler=handler,
        )

    return (
        definition(
            "procurement.list_my_requests",
            "List procurement requests owned by the current actor.",
            ListMyRequestsInput, ToolRisk.SENSITIVE_READ, list_requests,
        ),
        definition(
            "procurement.get_my_request",
            "Get one procurement request owned by the current actor by UUID.",
            GetMyRequestInput, ToolRisk.SENSITIVE_READ, get_request,
        ),
        definition(
            "procurement.calculate_request_total",
            "Calculate exact line subtotals and total from supplied quantities and prices.",
            CalculateRequestTotalInput, ToolRisk.READ, calculate,
        ),
        definition(
            "procurement.submit_request",
            "Create a submission proposal only; copy every user-supplied field without "
            "shortening or rewriting, preserve the complete item_name including trailing "
            "digits, and never omit a supplied specification. All request fields are required "
            "and confirmation executes the proposal.",
            SubmitRequestInput, ToolRisk.WRITE, write_guard,
        ),
        definition(
            "procurement.withdraw_request",
            "Create a withdrawal proposal only for the current actor's running request.",
            WithdrawRequestInput, ToolRisk.WRITE, write_guard,
        ),
        definition(
            "approval.list_my_pending_tasks",
            "List pending approval tasks authorized for the current actor.",
            ListMyPendingTasksInput, ToolRisk.SENSITIVE_READ, list_tasks,
        ),
        definition(
            "approval.get_task_detail",
            "Get one approval task authorized for the current actor by UUID.",
            GetTaskDetailInput, ToolRisk.SENSITIVE_READ, task_detail,
        ),
        definition(
            "approval.approve_task",
            "Create an approval proposal only after an authorized pending detail read.",
            ApproveTaskInput, ToolRisk.WRITE, write_guard,
        ),
        definition(
            "approval.reject_task",
            "Create a rejection proposal only after an authorized pending detail read; reason is required.",
            RejectTaskInput, ToolRisk.WRITE, write_guard,
        ),
    )


def propose_procurement_write(
    db: Session,
    definition: ToolDefinition,
    arguments: Mapping[str, object],
    context: ToolContext,
    *,
    procurement_runtime: ProcurementRuntimePort,
    approval_runtime: ApprovalRuntimePort,
    persist: PersistConfirmation,
) -> ConfirmationBlock:
    authorize_tool(definition, context)
    actor = db.get(User, context.actor_user_id)
    if actor is None or not actor.is_active:
        raise ToolError("authentication_required")
    try:
        parsed = definition.input_model.model_validate(dict(arguments))
        normalized = _json_payload(parsed.model_dump())
        if definition.name == "procurement.submit_request":
            submit = SubmitRequestInput.model_validate(parsed.model_dump())
            procurement_runtime.preflight_submit(db, actor=actor)
            calculated = calculate_total(submit.items)
            preview = {
                "type": "procurement_request",
                "title": submit.title,
                "purpose": submit.purpose,
                "needed_by_date": submit.needed_by_date.isoformat(),
                "currency": submit.currency.value,
                "item_count": len(submit.items),
                "total": str(calculated.total_amount),
            }
        elif definition.name == "procurement.withdraw_request":
            withdraw = WithdrawRequestInput.model_validate(parsed.model_dump())
            detail = procurement_runtime.get_request(
                db, actor=actor, request_id=withdraw.request_id
            )
            if str(_request_projection(detail).get("status")) not in {
                "running", "pending_manager", "pending_procurement"
            }:
                raise ToolError("procurement_request_state_conflict")
            preview = dict(detail)
        elif definition.name in {"approval.approve_task", "approval.reject_task"}:
            task_id = parsed.model_dump()["task_id"]
            detail = approval_runtime.get_task(
                db, actor=actor, task_id=task_id
            )
            if str(_task_projection(detail).get("status")) != "pending":
                raise ToolError("approval_task_state_conflict")
            preview = {
                "task_id": str(task_id),
                "status": "pending",
                "decision": (
                    "approve" if definition.name == "approval.approve_task"
                    else "reject"
                ),
            }
        else:
            raise ToolError("unknown_tool")
        return persist(definition.name, normalized, preview, context)
    except ValidationError:
        raise ToolError("invalid_tool_arguments") from None


def _facts_result(
    label: str, values: object, model_result: Mapping[str, object]
) -> dict[str, object]:
    return {
        "blocks": [{
            "type": "business_facts",
            "facts": [{"label": label, "value": values}],
            "queried_at": datetime.now(timezone.utc).isoformat(),
        }],
        "model_result": dict(model_result),
    }


def _request_projection(value: Mapping[str, object]) -> dict[str, object]:
    result = dict(value)
    summary = value.get("summary")
    if "status" not in result and isinstance(summary, Mapping):
        result["status"] = summary.get("status", "unknown")
    return result


def _task_projection(value: Mapping[str, object]) -> dict[str, object]:
    task = value.get("task")
    source = task if isinstance(task, Mapping) else value
    task_id = source.get("task_id")
    return {
        "task_id": str(task_id) if task_id is not None else None,
        "status": str(source.get("status", "unknown")),
        # A successful actor-scoped detail read is itself the authorization fact.
        "authorized": True,
        "detail": dict(value),
    }


def _json_payload(values: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values.items():
        if isinstance(value, (date, datetime)):
            result[key] = value.isoformat()
        elif isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, Decimal):
            result[key] = format(value, "f")
        elif isinstance(value, list):
            result[key] = [
                _json_payload(item) if isinstance(item, Mapping) else item
                for item in value
            ]
        elif hasattr(value, "value"):
            result[key] = value.value
        else:
            result[key] = value
    return result


__all__ = [
    "ApproveTaskInput", "CalculateRequestTotalInput", "GetMyRequestInput",
    "GetTaskDetailInput", "ListMyPendingTasksInput", "ListMyRequestsInput",
    "RejectTaskInput", "SubmitRequestInput", "WithdrawRequestInput",
    "build_procurement_tool_definitions", "propose_procurement_write",
]
