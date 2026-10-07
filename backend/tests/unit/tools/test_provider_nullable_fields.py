from __future__ import annotations

import httpx

from policy_api.tools.planner_client import ToolPlanningClient


def client_for(message: dict[str, object]) -> ToolPlanningClient:
    response = httpx.Response(
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
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    return ToolPlanningClient(
        base_url="https://models.example.test/v1",
        api_key="secret",
        model="chat-test",
        transport=httpx.MockTransport(lambda _request: response),
    )


def tool_schema() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "probe_get_server_time",
            "description": "Provider nullable-field contract test.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_client_accepts_empty_content_when_a_tool_call_is_present() -> None:
    message = {
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "probe_get_server_time",
                    "arguments": "{}",
                },
            }
        ],
    }

    with client_for(message) as client:
        turn = client.complete([], tools=[tool_schema()])

    assert turn.text is None
    assert turn.tool_calls[0].call_id == "call_1"


def test_client_accepts_null_tool_calls_on_a_final_text_response() -> None:
    with client_for({"content": "The fixed time is 12:00.", "tool_calls": None}) as client:
        turn = client.complete([], tools=[tool_schema()])

    assert turn.text == "The fixed time is 12:00."
    assert turn.tool_calls == ()
