from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from policy_api.models import StringEnum
from policy_api.tools.definitions import ToolContext, ToolDefinition
from policy_api.tools.registry import ToolRegistry


class FlowPhase(StringEnum):
    INITIAL = "initial"
    GATHERING = "gathering"
    READY_TO_PROPOSE = "ready_to_propose"
    RESPOND_ONLY = "respond_only"


@dataclass(slots=True)
class ToolFlowState:
    phase: FlowPhase = FlowPhase.INITIAL
    intent: str = "default"
    visible_tool_names: tuple[str, ...] = ()
    completed_read_names: set[str] = field(default_factory=set)
    fact_flags: dict[str, str | bool] = field(default_factory=dict)
    collected_argument_names: set[str] = field(default_factory=set)
    server_supplied_argument_names: set[str] = field(default_factory=set)
    required_next_tool_name: str | None = None
    current_date: date | None = None


@dataclass(frozen=True, slots=True)
class ToolFlowDirective:
    visible_tool_names: tuple[str, ...]
    control_payload: Mapping[str, object]
    server_supplied_argument_names: tuple[str, ...] = ()
    required_next_tool_name: str | None = None


class ToolFlowPolicy(Protocol):
    def start(
        self,
        user_text: str,
        context: ToolContext,
        registry: ToolRegistry,
    ) -> ToolFlowState: ...

    def before_model(self, state: ToolFlowState) -> ToolFlowDirective: ...

    def normalize_arguments(
        self,
        state: ToolFlowState,
        definition: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> dict[str, object]: ...

    def observe_read(
        self,
        state: ToolFlowState,
        definition: ToolDefinition,
        normalized_arguments: Mapping[str, object],
        model_result: Mapping[str, object],
    ) -> None: ...


class DefaultToolFlowPolicy:
    def start(
        self,
        _user_text: str,
        context: ToolContext,
        registry: ToolRegistry,
    ) -> ToolFlowState:
        visible_names: list[str] = []
        for tool in registry.provider_tools(role=context.role):
            function = tool.get("function")
            if not isinstance(function, dict):
                raise ValueError("invalid_provider_tool")
            provider_name = function.get("name")
            if not isinstance(provider_name, str):
                raise ValueError("invalid_provider_tool")
            visible_names.append(registry.resolve_provider_name(provider_name).name)
        return ToolFlowState(visible_tool_names=tuple(visible_names))

    def before_model(self, state: ToolFlowState) -> ToolFlowDirective:
        return ToolFlowDirective(
            visible_tool_names=state.visible_tool_names,
            control_payload={
                "intent": state.intent,
                "phase": state.phase.value,
                "current_date": (
                    state.current_date.isoformat() if state.current_date else None
                ),
                "completed_read_names": sorted(state.completed_read_names),
                "fact_flags": dict(sorted(state.fact_flags.items())),
                "collected_argument_names": sorted(
                    state.collected_argument_names
                ),
                "allowed_next_tool_names": list(state.visible_tool_names),
            },
            server_supplied_argument_names=tuple(sorted(
                state.server_supplied_argument_names
            )),
            required_next_tool_name=state.required_next_tool_name,
        )

    def normalize_arguments(
        self,
        _state: ToolFlowState,
        _definition: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        return dict(arguments)

    def observe_read(
        self,
        _state: ToolFlowState,
        _definition: ToolDefinition,
        _normalized_arguments: Mapping[str, object],
        _model_result: Mapping[str, object],
    ) -> None:
        return None
