from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

import httpx


logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(self, *, base_url: str, api_key: str, model: str, dimension: int,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = 2,
                 timeout: float = 30.0, query_prefix: str = "",
                 passage_prefix: str = "") -> None:
        self._model = model
        self._dimension = dimension
        self._sleep = sleep
        self._max_retries = max_retries
        self._query_prefix = query_prefix
        self._passage_prefix = passage_prefix
        self._client = httpx.Client(base_url=base_url.rstrip("/") + "/", transport=transport,
            headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)

    def __enter__(self) -> EmbeddingClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post("embeddings", json={"model": self._model, "input": list(texts)})
            except httpx.TimeoutException as exc:
                raise EmbeddingError("embedding_timeout") from exc
            if response.status_code != 429:
                break
            if attempt == self._max_retries:
                raise EmbeddingError("embedding_rate_limited")
            delay = 0.25 * (2 ** attempt)
            logger.info("Embedding request rate limited; retrying batch_size=%d attempt=%d", len(texts), attempt + 1)
            self._sleep(delay)
        assert response is not None
        if response.status_code >= 400:
            raise EmbeddingError("embedding_request_failed")
        try:
            rows = response.json()["data"]
            ordered = sorted(rows, key=lambda item: item["index"])
            vectors = [item["embedding"] for item in ordered]
            if len(vectors) != len(texts) or any(len(vector) != self._dimension for vector in vectors):
                raise ValueError
            return vectors
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError("embedding_response_invalid") from exc

    @staticmethod
    def _with_prefix(texts: Sequence[str], prefix: str) -> list[str]:
        if not prefix:
            return list(texts)
        return [text if text.startswith(prefix) else prefix + text for text in texts]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(self._with_prefix(texts, self._query_prefix))

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(self._with_prefix(texts, self._passage_prefix))
