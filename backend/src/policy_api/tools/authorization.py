from __future__ import annotations

from policy_api.tools.definitions import ToolContext, ToolDefinition
from policy_api.tools.errors import ToolError


def authorize_tool(definition: ToolDefinition, context: ToolContext) -> None:
    if context.role not in definition.allowed_roles:
        raise ToolError("tool_role_forbidden")
