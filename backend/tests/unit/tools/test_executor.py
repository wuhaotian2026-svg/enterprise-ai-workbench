from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from pydantic import BaseModel
import pytest

from policy_api.models import UserRole
from policy_api.tools.definitions import ToolContext, ToolDefinition, ToolRisk
from policy_api.tools.errors import ToolError
from policy_api.tools.executor import ToolExecutor
from policy_api.tools.registry import ToolRegistry


class BalanceInput(BaseModel):
    year: int


def make_definition(
    handler,
    *,
    risk: ToolRisk = ToolRisk.SENSITIVE_READ,
    confirmation: bool = False,
    timeout_seconds: float = 2.0,
) -> ToolDefinition:
    return ToolDefinition(
        name="hr.get_my_leave_balances",
        description="Get my balances.",
        input_model=BalanceInput,
        risk_level=risk,
        allowed_roles=frozenset({UserRole.EMPLOYEE}),
        requires_confirmation=confirmation,
        timeout_seconds=timeout_seconds,
        result_fields=frozenset({"available", "year"}),
        handler=handler,
    )


def test_executor_injects_actor_and_filters_result_fields() -> None:
    seen: dict[str, object] = {}

    def handler(context: ToolContext, input_data: BaseModel) -> Mapping[str, object]:
        seen["actor"] = context.actor_user_id
        seen["role"] = context.role
        return {"available": 6.5, "year": input_data.year, "internal_note": "hidden"}

    definition = make_definition(handler)
    executor = ToolExecutor(ToolRegistry([definition]))
    actor_id = uuid4()
    context = ToolContext(actor_user_id=actor_id, role=UserRole.EMPLOYEE)

    execution = executor.execute(definition.name, {"year": 2026}, context)

    assert seen == {"actor": actor_id, "role": UserRole.EMPLOYEE}
    assert execution.result == {"available": 6.5, "year": 2026}
    assert execution.timeout_seconds == 2.0
    assert execution.duration_ms >= 0


def test_executor_denies_role_extra_actor_field_unknown_and_unconfirmed_write() -> None:
    definition = make_definition(
        lambda _context, _input: {"available": 1, "year": 2026},
        risk=ToolRisk.WRITE,
        confirmation=True,
    )
    executor = ToolExecutor(ToolRegistry([definition]))

    with pytest.raises(ToolError, match="tool_role_forbidden"):
        executor.execute(
            definition.name,
            {"year": 2026},
            ToolContext(actor_user_id=uuid4(), role=UserRole.ADMIN),
            confirmed=True,
        )
    with pytest.raises(ToolError, match="invalid_tool_arguments"):
        executor.execute(
            definition.name,
            {"year": 2026, "employee_id": str(uuid4())},
            ToolContext(actor_user_id=uuid4(), role=UserRole.EMPLOYEE),
            confirmed=True,
        )
    with pytest.raises(ToolError, match="tool_confirmation_required"):
        executor.execute(
            definition.name,
            {"year": 2026},
            ToolContext(actor_user_id=uuid4(), role=UserRole.EMPLOYEE),
        )
    with pytest.raises(ToolError, match="unknown_tool"):
        executor.execute(
            "hr.unknown",
            {},
            ToolContext(actor_user_id=uuid4(), role=UserRole.EMPLOYEE),
        )


def test_executor_reports_bounded_timeout_without_returning_handler_data() -> None:
    ticks = iter([10.0, 10.2])
    definition = make_definition(
        lambda _context, _input: {"available": 1, "year": 2026},
        timeout_seconds=0.1,
    )
    executor = ToolExecutor(ToolRegistry([definition]), clock=lambda: next(ticks))

    with pytest.raises(ToolError) as raised:
        executor.execute(
            definition.name,
            {"year": 2026},
            ToolContext(actor_user_id=uuid4(), role=UserRole.EMPLOYEE),
        )

    assert raised.value.code == "tool_timeout"
    assert raised.value.metadata == {
        "tool_name": definition.name,
        "timeout_seconds": 0.1,
    }
