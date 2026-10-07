from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel

from policy_api.models import StringEnum, UserRole


class ToolRisk(StringEnum):
    READ = "read"
    SENSITIVE_READ = "sensitive_read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ToolContext:
    actor_user_id: UUID
    role: UserRole


ToolHandler = Callable[[ToolContext, BaseModel], Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    risk_level: ToolRisk
    allowed_roles: frozenset[UserRole]
    requires_confirmation: bool
    timeout_seconds: float
    result_fields: frozenset[str]
    handler: ToolHandler

    def __post_init__(self) -> None:
        if not self.name or not self.description:
            raise ValueError("invalid_tool_definition")
        if not self.allowed_roles or self.timeout_seconds <= 0:
            raise ValueError("invalid_tool_definition")
        if self.risk_level == ToolRisk.WRITE and not self.requires_confirmation:
            raise ValueError("write_tool_requires_confirmation")
