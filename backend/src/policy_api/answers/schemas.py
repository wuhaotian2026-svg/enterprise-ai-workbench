from __future__ import annotations

from dataclasses import dataclass

from policy_api.models import RefusalReason


@dataclass(frozen=True)
class EvidenceChunk:
    chunk_id: str
    text: str
    score: float
    source_active: bool


@dataclass(frozen=True)
class ConflictDecision:
    conflicting: bool
    categories: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceDecision:
    allowed: bool
    reason: RefusalReason | None
    score: float
    selected_chunks: tuple[EvidenceChunk, ...]


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    status: str
    text: str | None
    refusal_reason: RefusalReason | None
    evidence_score: float
    citations: tuple[str, ...]
    clarification_questions: tuple[str, ...] = ()
