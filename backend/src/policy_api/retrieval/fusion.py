from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from policy_api.retrieval.types import RetrievalCandidate, RetrievalResult


@dataclass(frozen=True, slots=True)
class RankedCandidateList:
    channel: Literal["lexical", "vector"]
    variant_index: int
    candidates: tuple[RetrievalCandidate, ...]


def reciprocal_rank_fusion(
    rankings: Sequence[RankedCandidateList],
    *,
    lexical_weight: float,
    vector_weight: float,
    rank_constant: int,
    top_k: int,
) -> list[RetrievalResult]:
    fused: dict[UUID, float] = {}
    base: dict[UUID, RetrievalCandidate] = {}
    lexical: dict[UUID, float] = {}
    vector: dict[UUID, float] = {}
    sources: dict[UUID, set[str]] = {}

    for ranking in rankings:
        weight = lexical_weight if ranking.channel == "lexical" else vector_weight
        for rank, candidate in enumerate(ranking.candidates, start=1):
            identifier = candidate.chunk_id
            base.setdefault(identifier, candidate)
            fused[identifier] = fused.get(identifier, 0.0) + weight / (
                rank_constant + rank
            )
            sources.setdefault(identifier, set()).add(ranking.channel)
            if candidate.lexical_score is not None:
                lexical[identifier] = max(
                    lexical.get(identifier, 0.0), candidate.lexical_score
                )
            if candidate.vector_score is not None:
                vector[identifier] = max(
                    vector.get(identifier, 0.0), candidate.vector_score
                )

    ordered = sorted(
        base,
        key=lambda identifier: (-fused[identifier], str(identifier)),
    )[:top_k]
    return [
        RetrievalResult(
            chunk_id=base[identifier].chunk_id,
            document_id=base[identifier].document_id,
            document_name=base[identifier].document_name,
            text=base[identifier].text,
            page=base[identifier].page,
            heading_path=base[identifier].heading_path,
            location=base[identifier].location,
            lexical_score=lexical.get(identifier),
            vector_score=vector.get(identifier),
            fused_score=fused[identifier],
            matched_by=tuple(
                name
                for name in ("lexical", "vector")
                if name in sources[identifier]
            ),
        )
        for identifier in ordered
    ]
