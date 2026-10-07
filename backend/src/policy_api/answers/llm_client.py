from __future__ import annotations

import json

import httpx

from policy_api.answers.model_output import (
    ModelAnswer,
    ModelAnswerError,
    parse_model_answer,
)



class AnswerModelClient:
    def __init__(self, *, base_url: str, api_key: str, model: str,
                 transport: httpx.BaseTransport | None = None, timeout: float = 30,
                 disable_thinking: bool = False) -> None:
        self.model = model
        self.disable_thinking = disable_thinking
        self.client = httpx.Client(base_url=base_url.rstrip("/") + "/", transport=transport,
            headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()
    def close(self) -> None: self.client.close()

    def generate(self, messages: list[dict[str, str]], *, allowed_citation_ids: set[str]) -> ModelAnswer:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        if self.disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        try:
            response = self.client.post("chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise ModelAnswerError("model_timeout") from exc
        if response.status_code == 429: raise ModelAnswerError("model_rate_limited")
        if response.status_code >= 400: raise ModelAnswerError("model_request_failed")
        try:
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") == "content_filter": raise ModelAnswerError("model_content_filtered")
            payload = json.loads(choice["message"]["content"])
            return parse_model_answer(
                payload, allowed_citation_ids=allowed_citation_ids
            )
        except ModelAnswerError: raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelAnswerError("model_output_invalid") from exc
