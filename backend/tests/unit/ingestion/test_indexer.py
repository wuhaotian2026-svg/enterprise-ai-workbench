from __future__ import annotations

import pytest

from policy_api.ingestion.embedding_client import EmbeddingError
from policy_api.ingestion.indexer import PreparedChunk, prepare_chunks
from policy_api.ingestion.types import ParsedBlock


def blocks(count: int) -> list[ParsedBlock]:
    return [ParsedBlock(text=f"第{index}条 制度内容。", page=index, heading_path=("制度",),
        location=f"page:{index}", block_type="paragraph") for index in range(1, count + 1)]


def test_prepare_chunks_embeds_in_bounded_batches_and_preserves_order() -> None:
    calls: list[list[str]] = []
    def embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts); return [[float(text[1]), 0.0, 0.0] for text in texts]
    prepared = prepare_chunks(blocks(5), embed=embed, batch_size=2, max_chars=100)
    assert [len(call) for call in calls] == [2, 2, 1]
    assert [chunk.sequence for chunk in prepared] == [0, 1, 2, 3, 4]
    assert [chunk.page for chunk in prepared] == [1, 2, 3, 4, 5]


def test_prepare_chunks_returns_nothing_when_third_embedding_batch_fails() -> None:
    calls = 0
    def embed(texts: list[str]) -> list[list[float]]:
        nonlocal calls; calls += 1
        if calls == 3: raise EmbeddingError("embedding_request_failed")
        return [[0.1, 0.2, 0.3] for _ in texts]
    with pytest.raises(EmbeddingError, match="embedding_request_failed"):
        prepare_chunks(blocks(5), embed=embed, batch_size=2, max_chars=100)
    assert calls == 3


def test_prepared_chunk_contains_no_mutable_shared_metadata() -> None:
    prepared = prepare_chunks(blocks(1), embed=lambda texts: [[0.1, 0.2, 0.3] for _ in texts], batch_size=4, max_chars=100)
    assert isinstance(prepared[0], PreparedChunk)
    assert prepared[0].heading_path == ("制度",)
