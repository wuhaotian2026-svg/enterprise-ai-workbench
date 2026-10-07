from __future__ import annotations

import json

import httpx
import pytest

from policy_api.answers.llm_client import AnswerModelClient, ModelAnswerError


def client(handler, *, disable_thinking: bool = False) -> AnswerModelClient:
    credential_placeholder = "-".join(("test", "placeholder"))
    return AnswerModelClient(base_url="https://models.example.test/v1", api_key=credential_placeholder, model="chat-test",
        transport=httpx.MockTransport(handler), timeout=1,
        disable_thinking=disable_thinking)


def response(content: str, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]})


def test_client_accepts_structured_answer_clarification_and_abstention() -> None:
    replies = iter([response('{"status":"answered","answer":"上限500元。","citations":["c1"]}'),
                    response('{"status":"needs_clarification","answer":"标准350元。","citations":["c1"],"clarification_questions":["住宿几晚？"]}'),
                    response('{"status":"abstained","answer":null,"citations":[]}')])
    with client(lambda _request: next(replies)) as model:
        answered = model.generate([{"role":"user","content":"data"}], allowed_citation_ids={"c1"})
        clarified = model.generate([{"role":"user","content":"data"}], allowed_citation_ids={"c1"})
        abstained = model.generate([{"role":"user","content":"data"}], allowed_citation_ids={"c1"})
    assert answered.status == "answered" and answered.citations == ("c1",)
    assert clarified.status == "needs_clarification"
    assert clarified.clarification_questions == ("住宿几晚？",)
    assert abstained.status == "abstained" and abstained.answer is None


def test_client_disables_thinking_without_changing_json_answer_contract() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return response(
            '{"status":"answered","answer":"上限500元。","citations":["c1"]}'
        )

    messages = [{"role": "user", "content": "trusted-json-data"}]
    with client(handler, disable_thinking=True) as model:
        result = model.generate(messages, allowed_citation_ids={"c1"})

    assert result.status == "answered"
    assert captured == [
        {
            "model": "chat-test",
            "messages": messages,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0,
        }
    ]


def test_client_omits_provider_specific_thinking_field_by_default() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return response('{"status":"abstained","answer":null,"citations":[]}')

    with client(handler) as model:
        model.generate([], allowed_citation_ids=set())

    assert "thinking" not in captured[0]


@pytest.mark.parametrize("content", [
    "not-json",
    '{"status":"unknown","answer":"x","citations":["c1"]}',
    '{"status":"answered","answer":"x","citations":[]}',
    '{"status":"answered","answer":"x","citations":["outside"]}',
    '{"status":"answered","answer":"x","citations":["c1"],"extra":true}',
])
def test_client_rejects_invalid_structured_output(content: str) -> None:
    with client(lambda _request: response(content)) as model:
        with pytest.raises(ModelAnswerError, match="model_output_invalid"):
            model.generate([{"role":"user","content":"data"}], allowed_citation_ids={"c1"})


def test_client_maps_timeout_rate_limit_and_content_filter() -> None:
    def timeout(request: httpx.Request): raise httpx.ReadTimeout("slow", request=request)
    with client(timeout) as model:
        with pytest.raises(ModelAnswerError, match="model_timeout"): model.generate([], allowed_citation_ids=set())
    with client(lambda _request: httpx.Response(429)) as model:
        with pytest.raises(ModelAnswerError, match="model_rate_limited"): model.generate([], allowed_citation_ids=set())
    with client(lambda _request: response("", "content_filter")) as model:
        with pytest.raises(ModelAnswerError, match="model_content_filtered"): model.generate([], allowed_citation_ids=set())
