from __future__ import annotations

import uuid

from policy_api.retrieval.fusion import RankedCandidateList, reciprocal_rank_fusion
from policy_api.retrieval.types import RetrievalCandidate


def candidate(
    name: str,
    *,
    lexical_score: float | None = None,
    vector_score: float | None = None,
) -> RetrievalCandidate:
    return RetrievalCandidate(chunk_id=uuid.uuid5(uuid.NAMESPACE_DNS, name), document_id=uuid.uuid4(),
        document_name=f"{name}.txt", text=f"{name} evidence", page=1, heading_path="Policy",
        location="page:1", lexical_score=lexical_score, vector_score=vector_score)


def test_rrf_combines_rankings_deduplicates_and_is_deterministic() -> None:
    shared_lexical = candidate("shared", lexical_score=0.61)
    shared_vector = candidate("shared", vector_score=0.91)
    lexical_only = candidate("lexical", lexical_score=0.8)
    vector_only = candidate("vector", vector_score=0.7)
    rankings = (
        RankedCandidateList("lexical", 0, (shared_lexical, lexical_only)),
        RankedCandidateList("vector", 0, (vector_only, shared_vector)),
        RankedCandidateList("lexical", 1, (shared_lexical,)),
    )
    first = reciprocal_rank_fusion(rankings, lexical_weight=1.0,
        vector_weight=1.0, rank_constant=60, top_k=3)
    second = reciprocal_rank_fusion(rankings, lexical_weight=1.0,
        vector_weight=1.0, rank_constant=60, top_k=3)
    assert [item.chunk_id for item in first] == [shared_lexical.chunk_id, vector_only.chunk_id, lexical_only.chunk_id]
    assert first == second
    assert len({item.chunk_id for item in first}) == 3
    assert first[0].matched_by == ("lexical", "vector")
    assert first[0].lexical_score == 0.61
    assert first[0].vector_score == 0.91


def test_rrf_respects_weights_and_top_k() -> None:
    lexical = candidate("lexical", lexical_score=1.0)
    vector = candidate("vector", vector_score=1.0)
    rankings = (
        RankedCandidateList("lexical", 0, (lexical,)),
        RankedCandidateList("vector", 0, (vector,)),
    )
    result = reciprocal_rank_fusion(rankings, lexical_weight=2.0,
        vector_weight=0.5, rank_constant=10, top_k=1)
    assert [item.chunk_id for item in result] == [lexical.chunk_id]
