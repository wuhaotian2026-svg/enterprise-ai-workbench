from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from policy_api.models import UserRole
from policy_api.procurement.tools import (
    ApproveTaskInput,
    CalculateRequestTotalInput,
    GetMyRequestInput,
    GetTaskDetailInput,
    ListMyPendingTasksInput,
    ListMyRequestsInput,
    RejectTaskInput,
    SubmitRequestInput,
    WithdrawRequestInput,
    build_procurement_tool_definitions,
    propose_procurement_write,
)
from policy_api.tools.definitions import ToolContext, ToolRisk
from policy_api.tools.errors import ToolError


FORBIDDEN_AUTHORITY_FIELDS = {
    "actor", "actor_id", "actor_user_id", "owner", "owner_id",
    "organization_id", "organization_unit_id", "assignee", "assignee_id",
    "assigned_user_id", "capability", "scope", "role",
}


class FakeProcurementRuntime:
    def preflight_submit(self, _db, **_kwargs):  # type: ignore[no-untyped-def]
        return object()

    def list_requests(self, _db, **kwargs):  # type: ignore[no-untyped-def]
        return {"items": [], "total": 0, **{k: kwargs[k] for k in ("offset", "limit")}}

    def get_request(self, _db, **_kwargs):  # type: ignore[no-untyped-def]
        return {"id": uuid4(), "status": "pending_manager", "title": "研发采购"}


class FakeApprovalRuntime:
    def list_tasks(self, _db, **_kwargs):  # type: ignore[no-untyped-def]
        return {"items": [], "offset": 0, "limit": 20, "total": 0}

    def get_task(self, _db, **_kwargs):  # type: ignore[no-untyped-def]
        return {"task_id": uuid4(), "status": "pending", "authorized": True}


def definitions():  # type: ignore[no-untyped-def]
    return build_procurement_tool_definitions(
        cast(Session, None),
        procurement_runtime=FakeProcurementRuntime(),
        approval_runtime=FakeApprovalRuntime(),
    )


def test_registry_has_exact_closed_tool_catalog_and_risks() -> None:
    values = definitions()
    assert tuple(item.name for item in values) == (
        "procurement.list_my_requests",
        "procurement.get_my_request",
        "procurement.calculate_request_total",
        "procurement.submit_request",
        "procurement.withdraw_request",
        "approval.list_my_pending_tasks",
        "approval.get_task_detail",
        "approval.approve_task",
        "approval.reject_task",
    )
    assert {item.name for item in values if item.risk_level is ToolRisk.WRITE} == {
        "procurement.submit_request", "procurement.withdraw_request",
        "approval.approve_task", "approval.reject_task",
    }
    assert all(
        item.requires_confirmation is (item.risk_level is ToolRisk.WRITE)
        for item in values
    )


@pytest.mark.parametrize(
    "model",
    (
        ListMyRequestsInput, GetMyRequestInput, CalculateRequestTotalInput,
        SubmitRequestInput, WithdrawRequestInput, ListMyPendingTasksInput,
        GetTaskDetailInput, ApproveTaskInput, RejectTaskInput,
    ),
)
@pytest.mark.parametrize("field", sorted(FORBIDDEN_AUTHORITY_FIELDS))
def test_tool_schemas_reject_authority_and_scope_fields(
    model: type[BaseModel], field: str,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({field: str(uuid4())})
    assert field not in model.model_json_schema().get("properties", {})


def test_calculation_is_exact_and_returns_canonical_decimal_strings() -> None:
    definition = next(
        item for item in definitions()
        if item.name == "procurement.calculate_request_total"
    )
    result = definition.handler(
        ToolContext(actor_user_id=uuid4(), role=UserRole.EMPLOYEE),
        CalculateRequestTotalInput.model_validate({
            "items": [
                {"quantity": "2.00", "estimated_unit_price": "12.345"},
                {"quantity": "3", "estimated_unit_price": "1.00"},
            ]
        }),
    )
    assert result["model_result"] == {
        "subtotals": ["24.69", "3.00"], "total": "27.69",
    }


def test_write_handlers_never_execute_and_proposer_revalidates_authoritative_state() -> None:
    calls = {"persist": 0}
    context = ToolContext(actor_user_id=uuid4(), role=UserRole.EMPLOYEE)
    actor = SimpleNamespace(id=context.actor_user_id, is_active=True, role=context.role)

    class Db:
        def get(self, _model, _identifier):  # type: ignore[no-untyped-def]
            return actor

    db = cast(Session, Db())
    withdraw = next(item for item in definitions() if item.name == "procurement.withdraw_request")
    with pytest.raises(ToolError, match="tool_confirmation_required"):
        withdraw.handler(context, WithdrawRequestInput(request_id=uuid4()))

    request_id = uuid4()

    def persist(
        _name: str, normalized: dict[str, object], preview: dict[str, object], _context,
    ):  # type: ignore[no-untyped-def]
        calls["persist"] += 1
        return SimpleNamespace(normalized=normalized, preview=preview)

    block = propose_procurement_write(
        db, withdraw, {"request_id": str(request_id)}, context,
        procurement_runtime=FakeProcurementRuntime(),
        approval_runtime=FakeApprovalRuntime(), persist=persist,
    )
    assert calls["persist"] == 1
    assert block.normalized == {"request_id": str(request_id)}

    class TerminalRuntime(FakeProcurementRuntime):
        def get_request(self, _db, **_kwargs):  # type: ignore[no-untyped-def]
            return {"id": request_id, "status": "approved"}

    with pytest.raises(ToolError, match="procurement_request_state_conflict"):
        propose_procurement_write(
            db, withdraw, {"request_id": str(request_id)}, context,
            procurement_runtime=TerminalRuntime(),
            approval_runtime=FakeApprovalRuntime(), persist=persist,
        )
    assert calls["persist"] == 1


def test_submit_schema_requires_all_business_fields_without_filling_any() -> None:
    with pytest.raises(ValidationError):
        SubmitRequestInput.model_validate({"title": "电脑"})
    valid = SubmitRequestInput.model_validate({
        "title": " 研发采购 ", "purpose": " 环境升级 ",
        "needed_by_date": date.today().isoformat(), "currency": "CNY",
        "items": [{
            "category_code": "it_equipment", "item_name": "工作站",
            "specification": None, "quantity": Decimal("1"), "unit": "台",
            "estimated_unit_price": Decimal("10000"),
        }],
    })
    assert valid.title == "研发采购" and valid.purpose == "环境升级"


def test_read_handlers_project_authoritative_nested_status_for_flow_policy() -> None:
    actor = SimpleNamespace(id=uuid4(), is_active=True, role=UserRole.EMPLOYEE)

    class Db:
        def get(self, _model, _identifier):  # type: ignore[no-untyped-def]
            return actor

    class NestedProcurement(FakeProcurementRuntime):
        def get_request(self, _db, **kwargs):  # type: ignore[no-untyped-def]
            return {
                "id": kwargs["request_id"],
                "summary": {"status": "pending_procurement", "title": "工作站"},
                "purpose": "研发",
            }

    class NestedApproval(FakeApprovalRuntime):
        def get_task(self, _db, **kwargs):  # type: ignore[no-untyped-def]
            return {
                "task": {"task_id": kwargs["task_id"], "status": "pending"},
                "subject": {"summary": {"status": "pending_procurement"}},
            }

    values = build_procurement_tool_definitions(
        cast(Session, Db()),
        procurement_runtime=NestedProcurement(),
        approval_runtime=NestedApproval(),
    )
    context = ToolContext(actor_user_id=actor.id, role=actor.role)
    request_id = uuid4()
    task_id = uuid4()
    request = next(item for item in values if item.name == "procurement.get_my_request")
    task = next(item for item in values if item.name == "approval.get_task_detail")

    request_result = request.handler(context, GetMyRequestInput(request_id=request_id))
    task_result = task.handler(context, GetTaskDetailInput(task_id=task_id))

    assert request_result["model_result"]["status"] == "pending_procurement"
    assert task_result["model_result"] == {
        "task_id": str(task_id),
        "status": "pending",
        "authorized": True,
        "detail": {
            "task": {"task_id": task_id, "status": "pending"},
            "subject": {"summary": {"status": "pending_procurement"}},
        },
    }


def test_submit_preflight_failure_persists_zero_confirmation() -> None:
    actor = SimpleNamespace(id=uuid4(), is_active=True, role=UserRole.EMPLOYEE)

    class Db:
        def get(self, _model, _identifier):  # type: ignore[no-untyped-def]
            return actor

    class FailingPreflight(FakeProcurementRuntime):
        def preflight_submit(self, _db, **_kwargs):  # type: ignore[no-untyped-def]
            raise ToolError("procurement_manager_capability_required")

    values = build_procurement_tool_definitions(
        cast(Session, Db()),
        procurement_runtime=FailingPreflight(),
        approval_runtime=FakeApprovalRuntime(),
    )
    submit = next(item for item in values if item.name == "procurement.submit_request")
    persisted = 0

    def persist(*_args):  # type: ignore[no-untyped-def]
        nonlocal persisted
        persisted += 1
        raise AssertionError("preflight failure must precede persistence")

    with pytest.raises(ToolError, match="procurement_manager_capability_required"):
        propose_procurement_write(
            cast(Session, Db()),
            submit,
            {
                "title": "研发采购",
                "purpose": "环境升级",
                "needed_by_date": date.today().isoformat(),
                "currency": "CNY",
                "items": [{
                    "category_code": "it_equipment",
                    "item_name": "工作站",
                    "specification": None,
                    "quantity": "1",
                    "unit": "台",
                    "estimated_unit_price": "10000",
                }],
            },
            ToolContext(actor_user_id=actor.id, role=actor.role),
            procurement_runtime=FailingPreflight(),
            approval_runtime=FakeApprovalRuntime(),
            persist=persist,
        )

    assert persisted == 0
