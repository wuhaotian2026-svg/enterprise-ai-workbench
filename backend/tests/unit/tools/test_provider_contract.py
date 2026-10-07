from __future__ import annotations

import json

import httpx
import pytest

from policy_api.tools.planner_client import PlannerError, ToolPlanningClient
from policy_api.tools.probe import PROBE_TOOL_NAME


def final_response() -> httpx.Response:
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
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "No tool is required.",
                        "tool_calls": [],
                    },
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def tool_schema(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Provider contract test.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_client_explicitly_disables_thinking_for_bounded_tool_turns() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return final_response()

    with ToolPlanningClient(
        base_url="https://models.example.test/v1",
        api_key="secret",
        model="chat-test",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.complete(
            [{"role": "user", "content": "hello"}],
            tools=[tool_schema("provider_safe_tool")],
        )

    assert captured["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(
    "name",
    [
        "hr.get_my_leave_balances",
        "contains space",
        "contains/slash",
        "x" * 65,
        "",
    ],
)
def test_client_rejects_provider_incompatible_function_names_before_http(name: str) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return final_response()

    with ToolPlanningClient(
        base_url="https://models.example.test/v1",
        api_key="secret",
        model="chat-test",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(PlannerError, match="tool_definition_invalid"):
            client.complete([], tools=[tool_schema(name)])

    assert requests == 0


def test_probe_uses_a_deepseek_compatible_provider_name() -> None:
    assert PROBE_TOOL_NAME == "probe_get_server_time"
