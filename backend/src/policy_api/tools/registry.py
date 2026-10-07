from __future__ import annotations

from collections.abc import Collection, Iterable
import re

from policy_api.models import UserRole
from policy_api.tools.definitions import ToolDefinition
from policy_api.tools.errors import ToolError


PROVIDER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ToolRegistry:
    def __init__(self, definitions: Iterable[ToolDefinition]) -> None:
        self._by_name: dict[str, ToolDefinition] = {}
        self._by_provider_name: dict[str, ToolDefinition] = {}
        for definition in definitions:
            if definition.name in self._by_name:
                raise ValueError("duplicate_tool_name")
            provider_name = self._provider_alias(definition.name)
            if provider_name in self._by_provider_name:
                raise ValueError("duplicate_provider_tool_name")
            if (
                UserRole.EMPLOYEE in definition.allowed_roles
                and "employee_id" in definition.input_model.model_fields
            ):
                raise ValueError("actor_field_forbidden")
            self._by_name[definition.name] = definition
            self._by_provider_name[provider_name] = definition

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._by_name[name]
        except KeyError:
            raise ToolError("unknown_tool") from None

    def provider_name(self, name: str) -> str:
        definition = self.get(name)
        return self._provider_alias(definition.name)

    def resolve_provider_name(self, provider_name: str) -> ToolDefinition:
        try:
            return self._by_provider_name[provider_name]
        except KeyError:
            raise ToolError("unknown_tool") from None

    def provider_tools(
        self,
        *,
        role: UserRole | None = None,
        names: Collection[str] | None = None,
    ) -> tuple[dict[str, object], ...]:
        selected_names = None if names is None else frozenset(names)
        if selected_names is not None:
            for name in selected_names:
                self.get(name)
        return tuple(
            {
                "type": "function",
                "function": {
                    "name": self._provider_alias(definition.name),
                    "description": definition.description,
                    "parameters": definition.input_model.model_json_schema(),
                },
            }
            for definition in self._by_name.values()
            if role is None or role in definition.allowed_roles
            if selected_names is None or definition.name in selected_names
        )

    @staticmethod
    def _provider_alias(internal_name: str) -> str:
        alias = internal_name.replace(".", "_")
        if not PROVIDER_NAME_PATTERN.fullmatch(alias):
            raise ValueError("invalid_provider_tool_name")
        return alias
