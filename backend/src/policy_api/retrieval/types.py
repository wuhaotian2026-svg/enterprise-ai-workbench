from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RetrievalCandidate:
    chunk_id: UUID
    document_id: UUID
    document_name: str
    text: str
    page: int | None
    heading_path: str | None
    location: str | None
    lexical_score: float | None = None
    vector_score: float | None = None


@dataclass(frozen=True)
class RetrievalResult(RetrievalCandidate):
    fused_score: float = 0.0
    matched_by: tuple[str, ...] = ()
