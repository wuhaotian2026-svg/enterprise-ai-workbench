from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from policy_api.retrieval.errors import RetrievalError
from policy_api.retrieval.fusion import RankedCandidateList, reciprocal_rank_fusion
from policy_api.retrieval.query_variants import QueryVariantBuilder
from policy_api.retrieval.types import RetrievalCandidate, RetrievalResult


class RetrievalRepository(Protocol):
    def lexical_search(self, query: str, limit: int) -> list[RetrievalCandidate]: ...
    def lexical_search_many(
        self,
        queries: tuple[str, ...],
        limit: int,
    ) -> list[list[RetrievalCandidate]]: ...
    def vector_search(self, embedding: list[float], limit: int) -> list[RetrievalCandidate]: ...


class RetrievalService:
    def __init__(
        self,
        *,
        repository: RetrievalRepository,
        embed: Callable[[list[str]], list[list[float]]],
        variant_builder: QueryVariantBuilder | None = None,
        lexical_candidate_limit: int = 20,
        vector_candidate_limit: int = 20,
        lexical_weight: float = 1.0,
        vector_weight: float = 1.0,
        rank_constant: int = 60,
        allow_vector_only_fallback: bool = False,
    ) -> None:
        self.repository = repository
        self.embed = embed
        self.variant_builder = variant_builder or QueryVariantBuilder()
        self.lexical_candidate_limit = lexical_candidate_limit
        self.vector_candidate_limit = vector_candidate_limit
        self.lexical_weight = lexical_weight
        self.vector_weight = vector_weight
        self.rank_constant = rank_constant
        self.allow_vector_only_fallback = allow_vector_only_fallback

    def retrieve(self, question: str, *, top_k: int) -> list[RetrievalResult]:
        variants = self.variant_builder.build(question)
        lexical_rankings: list[list[RetrievalCandidate]] | None = None
        lexical_error: Exception | None = None
        with ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="retrieval-embedding",
        ) as executor:
            embedding_future = executor.submit(self.embed, list(variants))
            try:
                lexical_rankings = self.repository.lexical_search_many(
                    variants,
                    self.lexical_candidate_limit,
                )
            except Exception as exc:
                lexical_error = exc
            try:
                embeddings = embedding_future.result()
            except Exception as exc:
                raise RetrievalError("query_embedding_failed") from exc

        try:
            embedding_count = len(embeddings)
        except TypeError as exc:
            raise RetrievalError("query_embedding_invalid") from exc
        if embedding_count != len(variants):
            raise RetrievalError("query_embedding_invalid")

        if lexical_error is not None:
            if not (
                self.allow_vector_only_fallback
                and isinstance(lexical_error, RetrievalError)
            ):
                raise lexical_error
            lexical_rankings = None
        if lexical_rankings is not None and len(lexical_rankings) != len(variants):
            raise RetrievalError("lexical_search_invalid")

        rankings: list[RankedCandidateList] = []
        for index, (variant, embedding) in enumerate(zip(variants, embeddings)):
            if lexical_rankings is not None:
                rankings.append(
                    RankedCandidateList(
                        "lexical",
                        index,
                        tuple(lexical_rankings[index]),
                    )
                )
            vector = self.repository.vector_search(
                embedding, self.vector_candidate_limit
            )
            rankings.append(RankedCandidateList("vector", index, tuple(vector)))

        return reciprocal_rank_fusion(
            rankings,
            lexical_weight=self.lexical_weight,
            vector_weight=self.vector_weight,
            rank_constant=self.rank_constant,
            top_k=top_k,
        )
