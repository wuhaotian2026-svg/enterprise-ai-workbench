from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

from policy_api.tools.types import PlannerTurn


PROBE_TOOL_NAME = "probe_get_server_time"
PROBE_ARGUMENTS: dict[str, object] = {"timezone": "Asia/Shanghai"}
PROBE_RESULT: dict[str, object] = {
    "server_time": "2026-08-16T12:00:00+08:00",
}
PROBE_TOOL_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": PROBE_TOOL_NAME,
        "description": "Return a fixed server time for protocol verification.",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "enum": ["Asia/Shanghai"],
                }
            },
            "required": ["timezone"],
            "additionalProperties": False,
        },
    },
}


class Planner(Protocol):
    def complete(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
    ) -> PlannerTurn: ...


class ProtocolProbeError(RuntimeError):
    """A stable, non-sensitive protocol probe failure."""


def run_probe(
    planner: Planner,
    *,
    emit: Callable[[str], None] = print,
) -> dict[str, bool]:
    messages: list[dict[str, object]] = [
        {
            "role": "user",
            "content": (
                "Use probe_get_server_time with timezone Asia/Shanghai, "
                "then report the returned fixed time."
            ),
        }
    ]
    first_turn = planner.complete(messages, tools=[PROBE_TOOL_SCHEMA])
    if not first_turn.tool_calls:
        raise ProtocolProbeError("tool_call_missing")
    if len(first_turn.tool_calls) != 1:
        raise ProtocolProbeError("unexpected_tool_call_count")

    tool_call = first_turn.tool_calls[0]
    if tool_call.name != PROBE_TOOL_NAME:
        raise ProtocolProbeError("unexpected_tool_name")
    if tool_call.arguments != PROBE_ARGUMENTS:
        raise ProtocolProbeError("unexpected_tool_arguments")

    messages.extend(
        [
            {
                "role": "assistant",
                "content": first_turn.text,
                "tool_calls": [
                    {
                        "id": tool_call.call_id,
                        "type": "function",
                        "function": {
                            "name": tool_call.name,
                            "arguments": json.dumps(
                                tool_call.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": tool_call.call_id,
                "content": json.dumps(
                    PROBE_RESULT,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
    )
    final_turn = planner.complete(messages, tools=[PROBE_TOOL_SCHEMA])
    if final_turn.tool_calls:
        raise ProtocolProbeError("unexpected_followup_tool_call")
    if final_turn.text is None or not final_turn.text.strip():
        raise ProtocolProbeError("final_answer_missing")

    capabilities = {
        "tools_accepted": True,
        "tool_calls_standard": True,
        "arguments_json": True,
        "tool_result_roundtrip": True,
        "final_answer_present": True,
    }
    for name, supported in capabilities.items():
        emit(f"{name}={str(supported).lower()}")
    return capabilities
