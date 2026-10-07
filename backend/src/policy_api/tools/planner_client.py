from __future__ import annotations

import json
import re
from typing import Any

import httpx

from policy_api.tools.types import PlannedToolCall, PlannerTurn


class PlannerError(RuntimeError):
    """Stable provider or protocol error safe for application-level mapping."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class ToolPlanningClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30,
    ) -> None:
        self.model = model
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def __enter__(self) -> ToolPlanningClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def complete(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        required_tool_name: str | None = None,
    ) -> PlannerTurn:
        available_tool_names = self._validate_tool_names(tools)
        if (
            required_tool_name is not None
            and required_tool_name not in available_tool_names
        ):
            raise PlannerError("tool_definition_invalid")
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "thinking": {"type": "disabled"},
            "temperature": 0,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = (
                "auto"
                if required_tool_name is None
                else {
                    "type": "function",
                    "function": {"name": required_tool_name},
                }
            )
        try:
            response = self.client.post(
                "chat/completions",
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise PlannerError("tool_provider_timeout") from exc

        if response.status_code == 429:
            raise PlannerError("tool_provider_rate_limited", status_code=429)
        if response.status_code >= 400:
            raise PlannerError(
                "tool_provider_unavailable",
                status_code=response.status_code,
            )

        try:
            message = response.json()["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError
            raw_content = message.get("content")
            raw_tool_calls = message.get("tool_calls", [])
            if raw_tool_calls is None:
                raw_tool_calls = []
            if not isinstance(raw_tool_calls, list):
                raise ValueError

            tool_calls = tuple(self._parse_tool_call(item) for item in raw_tool_calls)
            if raw_content is None:
                text = None
            elif isinstance(raw_content, str):
                text = raw_content if raw_content.strip() else None
            else:
                raise ValueError
            if text is None and not tool_calls:
                raise ValueError
            return PlannerTurn(text=text, tool_calls=tool_calls)
        except PlannerError:
            raise
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PlannerError("tool_output_invalid") from exc

    @staticmethod
    def _validate_tool_names(tools: list[dict[str, object]]) -> set[str]:
        names: set[str] = set()
        for tool in tools:
            function = tool.get("function")
            if not isinstance(function, dict):
                raise PlannerError("tool_definition_invalid")
            name = function.get("name")
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None:
                raise PlannerError("tool_definition_invalid")
            names.add(name)
        return names

    @staticmethod
    def _parse_tool_call(payload: Any) -> PlannedToolCall:
        if not isinstance(payload, dict) or payload.get("type") != "function":
            raise ValueError
        call_id = payload["id"]
        function = payload["function"]
        if not isinstance(call_id, str) or not call_id:
            raise ValueError
        if not isinstance(function, dict):
            raise ValueError
        name = function["name"]
        raw_arguments = function["arguments"]
        if not isinstance(name, str) or not name or not isinstance(raw_arguments, str):
            raise ValueError
        arguments = json.loads(raw_arguments)
        if not isinstance(arguments, dict):
            raise ValueError
        return PlannedToolCall(call_id=call_id, name=name, arguments=arguments)
