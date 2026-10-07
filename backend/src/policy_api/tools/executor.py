from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import perf_counter

from pydantic import ValidationError

from policy_api.tools.authorization import authorize_tool
from policy_api.tools.definitions import ToolContext
from policy_api.tools.errors import ToolError
from policy_api.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    tool_name: str
    result: Mapping[str, object]
    duration_ms: int
    timeout_seconds: float


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._registry = registry
        self._clock = clock

    def execute(
        self,
        tool_name: str,
        raw_arguments: Mapping[str, object],
        context: ToolContext,
        *,
        confirmed: bool = False,
    ) -> ToolExecutionResult:
        definition = self._registry.get(tool_name)
        authorize_tool(definition, context)
        if definition.requires_confirmation and not confirmed:
            raise ToolError("tool_confirmation_required")
        if set(raw_arguments) - set(definition.input_model.model_fields):
            raise ToolError("invalid_tool_arguments")
        try:
            input_data = definition.input_model.model_validate(dict(raw_arguments))
        except ValidationError:
            raise ToolError("invalid_tool_arguments") from None

        started_at = self._clock()
        try:
            raw_result = definition.handler(context, input_data)
        except ToolError:
            raise
        except Exception:
            raise ToolError("tool_execution_failed") from None
        duration_ms = max(0, round((self._clock() - started_at) * 1000))
        if duration_ms > definition.timeout_seconds * 1000:
            raise ToolError(
                "tool_timeout",
                metadata={
                    "tool_name": definition.name,
                    "timeout_seconds": definition.timeout_seconds,
                },
            )
        result = {
            key: value
            for key, value in raw_result.items()
            if key in definition.result_fields
        }
        return ToolExecutionResult(
            tool_name=definition.name,
            result=result,
            duration_ms=duration_ms,
            timeout_seconds=definition.timeout_seconds,
        )
