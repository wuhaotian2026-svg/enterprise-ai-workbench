from __future__ import annotations

import json

import pytest

from policy_api.tools.probe import ProtocolProbeError, run_probe
from policy_api.tools.types import PlannedToolCall, PlannerTurn


class RecordingPlanner:
    def __init__(self, replies: list[PlannerTurn]) -> None:
        self.replies = iter(replies)
        self.messages: list[list[dict[str, object]]] = []
        self.tools: list[list[dict[str, object]]] = []

    def complete(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
    ) -> PlannerTurn:
        self.messages.append(messages)
        self.tools.append(tools)
        return next(self.replies)


def test_probe_round_trips_the_standard_tool_call_id_and_reports_only_capabilities() -> None:
    planner = RecordingPlanner(
        [
            PlannerTurn(
                text=None,
                tool_calls=(
                    PlannedToolCall(
                        call_id="call_probe_1",
                        name="probe_get_server_time",
                        arguments={"timezone": "Asia/Shanghai"},
                    ),
                ),
            ),
            PlannerTurn(text="The server time is 12:00.", tool_calls=()),
        ]
    )
    emitted: list[str] = []

    result = run_probe(planner, emit=emitted.append)

    assert result == {
        "tools_accepted": True,
        "tool_calls_standard": True,
        "arguments_json": True,
        "tool_result_roundtrip": True,
        "final_answer_present": True,
    }
    assert emitted == [
        "tools_accepted=true",
        "tool_calls_standard=true",
        "arguments_json=true",
        "tool_result_roundtrip=true",
        "final_answer_present=true",
    ]
    second_messages = planner.messages[1]
    assert second_messages[-1] == {
        "role": "tool",
        "tool_call_id": "call_probe_1",
        "content": json.dumps(
            {"server_time": "2026-08-16T12:00:00+08:00"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


@pytest.mark.parametrize(
    ("turn", "error_code"),
    [
        (PlannerTurn(text="I will not use a tool.", tool_calls=()), "tool_call_missing"),
        (
            PlannerTurn(
                text=None,
                tool_calls=(
                    PlannedToolCall("call_1", "probe_get_server_time", {"timezone": "Asia/Shanghai"}),
                    PlannedToolCall("call_2", "probe_get_server_time", {"timezone": "Asia/Shanghai"}),
                ),
            ),
            "unexpected_tool_call_count",
        ),
        (
            PlannerTurn(
                text=None,
                tool_calls=(PlannedToolCall("call_1", "unknown.tool", {}),),
            ),
            "unexpected_tool_name",
        ),
        (
            PlannerTurn(
                text=None,
                tool_calls=(
                    PlannedToolCall(
                        "call_1",
                        "probe_get_server_time",
                        {"timezone": "UTC"},
                    ),
                ),
            ),
            "unexpected_tool_arguments",
        ),
    ],
)
def test_probe_fails_closed_for_nonstandard_or_unexpected_proposals(
    turn: PlannerTurn,
    error_code: str,
) -> None:
    planner = RecordingPlanner([turn])

    with pytest.raises(ProtocolProbeError, match=error_code):
        run_probe(planner, emit=lambda _line: None)
