from __future__ import annotations

import uuid
from threading import Event, current_thread, get_ident

import pytest

from policy_api.retrieval.query_variants import QueryVariantBuilder
from policy_api.retrieval.service import RetrievalError, RetrievalService
from policy_api.retrieval.types import RetrievalCandidate


def item(name: str) -> RetrievalCandidate:
    return RetrievalCandidate(chunk_id=uuid.uuid5(uuid.NAMESPACE_DNS, name), document_id=uuid.uuid4(),
        document_name=f"{name}.txt", text=f"{name} evidence", page=2,
        heading_path="Leave", location="page:2", lexical_score=0.8, vector_score=0.8)


class Repository:
    def __init__(self, lexical=None, vector=None): self.lexical = lexical or []; self.vector = vector or []; self.calls = []
    def lexical_search(self, query: str, limit: int): self.calls.append(("lexical", query, limit)); return self.lexical
    def lexical_search_many(self, queries: tuple[str, ...], limit: int):
        self.calls.append(("lexical_many", queries, limit))
        return [list(self.lexical) for _query in queries]
    def vector_search(self, embedding: list[float], limit: int): self.calls.append(("vector", embedding, limit)); return self.vector


def test_retrieval_service_rejects_empty_question_without_embedding() -> None:
    repository = Repository(); embedded = False
    def embed(_texts):
        nonlocal embedded; embedded = True; return [[0.1]]
    service = RetrievalService(repository=repository, embed=embed)
    with pytest.raises(RetrievalError, match="empty_question"):
        service.retrieve("   ", top_k=5)
    assert not embedded and repository.calls == []


def test_retrieval_service_propagates_stable_embedding_failure() -> None:
    service = RetrievalService(repository=Repository(), embed=lambda _texts: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RetrievalError, match="query_embedding_failed"):
        service.retrieve("休假如何申请", top_k=5)


def test_retrieval_service_returns_empty_for_no_corpus_and_preserves_metadata() -> None:
    empty = RetrievalService(repository=Repository(), embed=lambda _texts: [[0.1, 0.2]])
    assert empty.retrieve("休假", top_k=5) == []
    shared = item("shared")
    repository = Repository(lexical=[shared], vector=[shared])
    result = RetrievalService(repository=repository, embed=lambda _texts: [[0.1, 0.2]]).retrieve("休假", top_k=5)
    assert result[0].document_name == "shared.txt"
    assert result[0].page == 2 and result[0].heading_path == "Leave" and result[0].location == "page:2"


def test_retrieve_embeds_all_variants_once_and_preserves_channel_scores() -> None:
    shared_lexical = item("shared")
    shared_lexical = RetrievalCandidate(
        **{**shared_lexical.__dict__, "vector_score": None}
    )
    shared_vector = RetrievalCandidate(
        **{**shared_lexical.__dict__, "lexical_score": None, "vector_score": 0.93}
    )
    repository = Repository(lexical=[shared_lexical], vector=[shared_vector])
    embed_calls: list[list[str]] = []
    builder = QueryVariantBuilder(max_count=3)
    service = RetrievalService(
        repository=repository,
        embed=lambda texts: embed_calls.append(texts) or [[0.1] for _ in texts],
        variant_builder=builder,
        lexical_candidate_limit=8,
        vector_candidate_limit=9,
    )

    results = service.retrieve("请问南京出差三天多少钱", top_k=6)

    variants = list(builder.build("请问南京出差三天多少钱"))
    assert embed_calls == [variants]
    assert repository.calls == [
        ("lexical_many", tuple(variants), 8),
        *[("vector", [0.1], 9) for _variant in variants],
    ]
    assert results[0].lexical_score == 0.8
    assert results[0].vector_score == 0.93


def test_retrieve_overlaps_embedding_with_lexical_search_on_caller_thread() -> None:
    caller_thread_id = get_ident()
    lexical_started = Event()
    embedding_threads = []

    class OverlapRepository(Repository):
        def lexical_search_many(self, queries: tuple[str, ...], limit: int):
            assert get_ident() == caller_thread_id
            lexical_started.set()
            return [[] for _query in queries]

        def vector_search(self, embedding: list[float], limit: int):
            assert get_ident() == caller_thread_id
            return []

    def embed(texts: list[str]) -> list[list[float]]:
        embedding_threads.append(current_thread())
        if not lexical_started.wait(timeout=1.0):
            raise AssertionError("lexical search did not overlap embedding")
        return [[0.1] for _text in texts]

    service = RetrievalService(repository=OverlapRepository(), embed=embed)

    assert service.retrieve("休假如何申请", top_k=5) == []
    assert len(embedding_threads) == 1
    assert embedding_threads[0].ident != caller_thread_id
    assert not embedding_threads[0].is_alive()


def test_embedding_failure_wins_when_concurrent_lexical_search_also_fails() -> None:
    lexical_started = Event()

    class FailingRepository(Repository):
        def __init__(self) -> None:
            super().__init__()
            self.lexical_called = False

        def lexical_search_many(self, queries: tuple[str, ...], limit: int):
            self.lexical_called = True
            lexical_started.set()
            raise RetrievalError("lexical_search_failed")

    repository = FailingRepository()

    def embed(_texts: list[str]) -> list[list[float]]:
        if not lexical_started.wait(timeout=1.0):
            raise AssertionError("lexical search did not overlap embedding")
        raise RuntimeError("embedding unavailable")

    service = RetrievalService(repository=repository, embed=embed)

    with pytest.raises(RetrievalError, match="query_embedding_failed"):
        service.retrieve("休假如何申请", top_k=5)
    assert repository.lexical_called


def test_invalid_embedding_count_wins_when_concurrent_lexical_search_fails() -> None:
    lexical_started = Event()

    class FailingRepository(Repository):
        def __init__(self) -> None:
            super().__init__()
            self.lexical_called = False

        def lexical_search_many(self, queries: tuple[str, ...], limit: int):
            self.lexical_called = True
            lexical_started.set()
            raise RetrievalError("lexical_search_failed")

    repository = FailingRepository()

    def embed(_texts: list[str]) -> list[list[float]]:
        if not lexical_started.wait(timeout=1.0):
            raise AssertionError("lexical search did not overlap embedding")
        return []

    service = RetrievalService(repository=repository, embed=embed)

    with pytest.raises(RetrievalError, match="query_embedding_invalid"):
        service.retrieve("休假如何申请", top_k=5)
    assert repository.lexical_called


def test_retrieval_rejects_embedding_count_mismatch_and_lexical_failure() -> None:
    variants = QueryVariantBuilder().build("请问报销多少钱")
    service = RetrievalService(
        repository=Repository(),
        embed=lambda _texts: [[0.1]] * (len(variants) - 1),
        variant_builder=QueryVariantBuilder(),
    )
    with pytest.raises(RetrievalError, match="query_embedding_invalid"):
        service.retrieve("请问报销多少钱", top_k=5)

    class FailingRepository(Repository):
        def lexical_search_many(self, queries: tuple[str, ...], limit: int):
            raise RetrievalError("lexical_search_failed")

    strict = RetrievalService(repository=FailingRepository(), embed=lambda _texts: [[0.1]])
    with pytest.raises(RetrievalError, match="lexical_search_failed"):
        strict.retrieve("休假", top_k=5)

    fallback = RetrievalService(repository=FailingRepository(vector=[item("vector")]),
        embed=lambda _texts: [[0.1]], allow_vector_only_fallback=True)
    assert fallback.retrieve("休假", top_k=5)[0].matched_by == ("vector",)
