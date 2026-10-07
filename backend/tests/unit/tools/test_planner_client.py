from __future__ import annotations

import json

import httpx
import pytest

from policy_api.tools.planner_client import PlannerError, ToolPlanningClient


TOOL_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "probe_get_server_time",
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


def build_client(handler) -> ToolPlanningClient:  # type: ignore[no-untyped-def]
    return ToolPlanningClient(
        base_url="https://models.example.test/v1",
        api_key="secret",
        model="chat-test",
        transport=httpx.MockTransport(handler),
        timeout=1,
    )


def model_response(message: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1_723_811_200,
            "model": "chat-test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    "message": {"role": "assistant", **message},
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        },
    )


def test_client_sends_standard_tools_and_parses_a_tool_call() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return model_response(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "probe_get_server_time",
                            "arguments": '{"timezone":"Asia/Shanghai"}',
                        },
                    }
                ],
            }
        )

    with build_client(handler) as client:
        turn = client.complete(
            [{"role": "user", "content": "What time is it?"}],
            tools=[TOOL_SCHEMA],
        )

    assert captured == {
        "model": "chat-test",
        "messages": [{"role": "user", "content": "What time is it?"}],
        "tools": [TOOL_SCHEMA],
        "tool_choice": "auto",
        "thinking": {"type": "disabled"},
        "temperature": 0,
    }
    assert turn.text is None
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].call_id == "call_1"
    assert turn.tool_calls[0].name == "probe_get_server_time"
    assert turn.tool_calls[0].arguments == {"timezone": "Asia/Shanghai"}


def test_client_forces_named_tool_choice_when_requested() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return model_response(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_forced",
                        "type": "function",
                        "function": {
                            "name": "probe_get_server_time",
                            "arguments": '{"timezone":"Asia/Shanghai"}',
                        },
                    }
                ],
            }
        )

    with build_client(handler) as client:
        turn = client.complete(
            [{"role": "user", "content": "Read the server time."}],
            tools=[TOOL_SCHEMA],
            required_tool_name="probe_get_server_time",
        )

    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "probe_get_server_time"},
    }
    assert turn.tool_calls[0].name == "probe_get_server_time"


def test_client_rejects_required_tool_not_present_in_schema() -> None:
    with build_client(lambda _request: model_response({"content": "unused"})) as client:
        with pytest.raises(PlannerError, match="tool_definition_invalid"):
            client.complete(
                [],
                tools=[TOOL_SCHEMA],
                required_tool_name="missing_tool",
            )


def test_client_parses_multiple_standard_tool_calls_without_executing_them() -> None:
    reply = model_response(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "probe_get_server_time",
                        "arguments": '{"timezone":"Asia/Shanghai"}',
                    },
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "probe_get_server_time",
                        "arguments": '{"timezone":"Asia/Shanghai"}',
                    },
                },
            ],
        }
    )

    with build_client(lambda _request: reply) as client:
        turn = client.complete([], tools=[TOOL_SCHEMA])

    assert [call.call_id for call in turn.tool_calls] == ["call_1", "call_2"]


def test_client_parses_final_text_after_a_tool_result_round_trip() -> None:
    captured_messages: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_messages.extend(json.loads(request.content)["messages"])
        return model_response({"content": "The fixed server time is 12:00.", "tool_calls": []})

    messages: list[dict[str, object]] = [
        {"role": "user", "content": "What time is it?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "probe_get_server_time",
                        "arguments": '{"timezone":"Asia/Shanghai"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"server_time":"2026-08-16T12:00:00+08:00"}',
        },
    ]

    with build_client(handler) as client:
        turn = client.complete(messages, tools=[TOOL_SCHEMA])

    assert captured_messages == messages
    assert turn.text == "The fixed server time is 12:00."
    assert turn.tool_calls == ()


def test_client_omits_tool_fields_for_a_respond_only_round() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return model_response({"content": "Final answer.", "tool_calls": []})

    with build_client(handler) as client:
        turn = client.complete(
            [{"role": "user", "content": "Answer from existing facts."}],
            tools=[],
        )

    assert turn.text == "Final answer."
    assert captured == {
        "model": "chat-test",
        "messages": [
            {"role": "user", "content": "Answer from existing facts."}
        ],
        "thinking": {"type": "disabled"},
        "temperature": 0,
    }


@pytest.mark.parametrize(
    "tool_call",
    [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "probe_get_server_time", "arguments": "not-json"},
        },
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "probe_get_server_time", "arguments": "[]"},
        },
        {
            "id": "",
            "type": "function",
            "function": {
                "name": "probe_get_server_time",
                "arguments": '{"timezone":"Asia/Shanghai"}',
            },
        },
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "", "arguments": '{"timezone":"Asia/Shanghai"}'},
        },
    ],
)
def test_client_rejects_malformed_tool_calls(tool_call: dict[str, object]) -> None:
    reply = model_response({"content": None, "tool_calls": [tool_call]})

    with build_client(lambda _request: reply) as client:
        with pytest.raises(PlannerError, match="tool_output_invalid"):
            client.complete([], tools=[TOOL_SCHEMA])


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": 42, "tool_calls": []}}]},
    ],
)
def test_client_rejects_invalid_provider_response(payload: dict[str, object]) -> None:
    with build_client(lambda _request: httpx.Response(200, json=payload)) as client:
        with pytest.raises(PlannerError, match="tool_output_invalid"):
            client.complete([], tools=[TOOL_SCHEMA])


def test_client_maps_timeout_rate_limit_and_provider_failure() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with build_client(timeout) as client:
        with pytest.raises(PlannerError, match="tool_provider_timeout"):
            client.complete([], tools=[TOOL_SCHEMA])

    with build_client(lambda _request: httpx.Response(429)) as client:
        with pytest.raises(PlannerError, match="tool_provider_rate_limited"):
            client.complete([], tools=[TOOL_SCHEMA])

    with build_client(lambda _request: httpx.Response(503)) as client:
        with pytest.raises(PlannerError, match="tool_provider_unavailable"):
            client.complete([], tools=[TOOL_SCHEMA])
