from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class QuestionCreate(BaseModel):
    question_id: UUID | None = None
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value: raise ValueError("question_must_not_be_blank")
        return value


class CitationResponse(BaseModel):
    number: int
    chunk_id: UUID
    evidence_snapshot: str
    source_status: str
    document_name: str
    mime_type: str
    page_number: int | None
    heading_path: str | None
    location: str | None


class ClarificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[str] = Field(min_length=1, max_length=3)


class AnswerResponse(BaseModel):
    id: UUID
    status: str
    text: str | None
    refusal_reason: str | None
    evidence_score: float | None
    citations: list[CitationResponse]
    clarification: ClarificationResponse | None = None


class QuestionResponse(BaseModel):
    id: UUID
    text: str
    status: str
    created_at: datetime
    answer: AnswerResponse | None


class QuestionSummary(BaseModel):
    id: UUID
    text: str
    status: str
    created_at: datetime
