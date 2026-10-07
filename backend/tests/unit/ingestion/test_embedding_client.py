from __future__ import annotations

import logging
import json

import httpx
import pytest

from policy_api.ingestion.embedding_client import EmbeddingClient, EmbeddingError


def client(handler, *, sleeps: list[float] | None = None) -> EmbeddingClient:
    transport = httpx.MockTransport(handler)
    return EmbeddingClient(base_url="https://models.example.test/v1", api_key="super-secret-key",
        model="embedding-test", dimension=3, transport=transport,
        sleep=(sleeps.append if sleeps is not None else lambda _seconds: None), max_retries=2)


def test_embedding_client_sends_one_batch_and_orders_vectors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer super-secret-key"
        assert request.read().decode().count("policy") == 2
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0.4, 0.5, 0.6]},
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
        ]})
    with client(handler) as embeddings:
        assert embeddings.embed(["policy one", "policy two"]) == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


def test_embedding_client_retries_429_with_finite_backoff() -> None:
    attempts = 0; sleeps: list[float] = []
    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts; attempts += 1
        if attempts < 3: return httpx.Response(429, json={"error": "limited"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]})
    with client(handler, sleeps=sleeps) as embeddings:
        assert embeddings.embed(["policy"]) == [[0.1, 0.2, 0.3]]
    assert attempts == 3 and sleeps == [0.25, 0.5]


@pytest.mark.parametrize("response", [
    {"data": []},
    {"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
])
def test_embedding_client_rejects_count_or_dimension_mismatch(response: dict) -> None:
    with client(lambda _request: httpx.Response(200, json=response)) as embeddings:
        with pytest.raises(EmbeddingError, match="embedding_response_invalid"):
            embeddings.embed(["policy"])


def test_embedding_client_does_not_retry_non_retryable_error_or_log_secrets(caplog: pytest.LogCaptureFixture) -> None:
    full_text = "完整制度正文不得写入日志"
    caplog.set_level(logging.INFO)
    attempts = 0
    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts; attempts += 1
        return httpx.Response(400, json={"error": "bad request"})
    with client(handler) as embeddings:
        with pytest.raises(EmbeddingError, match="embedding_request_failed"):
            embeddings.embed([full_text])
    assert attempts == 1
    assert "super-secret-key" not in caplog.text and full_text not in caplog.text


def test_embedding_client_converts_timeout_to_stable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)
    with client(handler) as embeddings:
        with pytest.raises(EmbeddingError, match="embedding_timeout"):
            embeddings.embed(["policy"])


def test_embedding_client_applies_query_and_passage_prefixes_without_double_prefixing() -> None:
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.read())["input"])
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": [0.1, 0.2, 0.3]}
            for index, _text in enumerate(requests[-1])
        ]})

    embeddings = EmbeddingClient(
        base_url="https://models.example.test/v1",
        api_key="local-only",
        model="intfloat/multilingual-e5-small",
        dimension=3,
        transport=httpx.MockTransport(handler),
        query_prefix="query: ",
        passage_prefix="passage: ",
    )
    with embeddings:
        embeddings.embed_queries(["请假需要提前多久？", "query: 已有前缀"])
        embeddings.embed_passages(["员工应提前提交申请。", "passage: 已有前缀"])

    assert requests == [
        ["query: 请假需要提前多久？", "query: 已有前缀"],
        ["passage: 员工应提前提交申请。", "passage: 已有前缀"],
    ]
