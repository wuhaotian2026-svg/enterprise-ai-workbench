from __future__ import annotations

import httpx
import pytest

from policy_api.tools.planner_client import PlannerError, ToolPlanningClient


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422, 500, 503])
def test_provider_http_error_retains_only_the_status_code(status_code: int) -> None:
    client = ToolPlanningClient(
        base_url="https://models.example.test/v1",
        api_key="secret",
        model="chat-test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(status_code, text="sensitive body")),
    )

    with client, pytest.raises(PlannerError) as captured:
        client.complete([], tools=[])

    assert captured.value.code == "tool_provider_unavailable"
    assert captured.value.status_code == status_code
    assert str(captured.value) == "tool_provider_unavailable"
    assert "sensitive body" not in str(captured.value)
