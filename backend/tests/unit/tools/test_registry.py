from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

from pydantic import BaseModel
import pytest

from policy_api.models import UserRole
from policy_api.tools.definitions import ToolContext, ToolDefinition, ToolRisk
from policy_api.tools.errors import ToolError
from policy_api.tools.registry import ToolRegistry


class EmptyInput(BaseModel):
    pass


class UnsafeEmployeeInput(BaseModel):
    employee_id: UUID


def handler(_context: ToolContext, _input: BaseModel) -> Mapping[str, object]:
    return {"ok": True}


def definition(
    name: str = "hr.get_my_leave_balances",
    *,
    input_model: type[BaseModel] = EmptyInput,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Get the actor's leave balances.",
        input_model=input_model,
        risk_level=ToolRisk.SENSITIVE_READ,
        allowed_roles=frozenset({UserRole.EMPLOYEE, UserRole.HR}),
        requires_confirmation=False,
        timeout_seconds=2.0,
        result_fields=frozenset({"ok"}),
        handler=handler,
    )


def test_registry_rejects_duplicate_internal_or_provider_names() -> None:
    with pytest.raises(ValueError, match="duplicate_tool_name"):
        ToolRegistry([definition(), definition()])

    with pytest.raises(ValueError, match="duplicate_provider_tool_name"):
        ToolRegistry([definition("hr.a_b"), definition("hr_a.b")])


def test_registry_maps_internal_names_to_provider_safe_aliases_and_denies_unknown() -> None:
    registry = ToolRegistry([definition()])

    assert registry.provider_name("hr.get_my_leave_balances") == "hr_get_my_leave_balances"
    assert registry.resolve_provider_name("hr_get_my_leave_balances").name == (
        "hr.get_my_leave_balances"
    )
    with pytest.raises(ToolError, match="unknown_tool"):
        registry.get("hr.delete_everything")


def test_employee_facing_tool_schema_cannot_expose_employee_id() -> None:
    with pytest.raises(ValueError, match="actor_field_forbidden"):
        ToolRegistry([definition(input_model=UnsafeEmployeeInput)])


def test_provider_tools_filters_by_internal_name_without_changing_order() -> None:
    registry = ToolRegistry(
        [
            definition("hr.first"),
            definition("hr.second"),
            definition("hr.third"),
        ]
    )

    tools = registry.provider_tools(
        role=UserRole.EMPLOYEE,
        names=("hr.third", "hr.first"),
    )

    assert [tool["function"]["name"] for tool in tools] == [
        "hr_first",
        "hr_third",
    ]


def test_provider_tools_name_filter_cannot_expand_role_access() -> None:
    employee_tool = definition("hr.employee")
    hr_only_tool = replace(
        employee_tool,
        name="hr.hr_only",
        allowed_roles=frozenset({UserRole.HR}),
    )
    registry = ToolRegistry([employee_tool, hr_only_tool])

    tools = registry.provider_tools(
        role=UserRole.EMPLOYEE,
        names=("hr.employee", "hr.hr_only"),
    )

    assert [tool["function"]["name"] for tool in tools] == ["hr_employee"]


def test_provider_tools_rejects_unknown_requested_internal_name() -> None:
    registry = ToolRegistry([definition()])

    with pytest.raises(ToolError, match="unknown_tool"):
        registry.provider_tools(names=("hr.missing",))
