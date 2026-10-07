from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)


_SENSITIVE_QUESTION = re.compile(
    r"(?:身份证(?:号|号码)?|银行卡(?:号|号码)?|密码|口令|密钥|"
    r"\bpassword\b|\btoken\b|\bapi\s*key\b|\bsecret\s*key\b)",
    re.IGNORECASE,
)


class ModelAnswerError(RuntimeError):
    pass


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _clean_nonempty(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("model_output_empty_string")
    return cleaned


def _clean_citations(values: list[str]) -> list[str]:
    cleaned = [_clean_nonempty(value) for value in values]
    return list(dict.fromkeys(cleaned))


class AnsweredModelAnswer(ClosedModel):
    status: Literal["answered"]
    answer: str = Field(min_length=1)
    citations: list[str] = Field(min_length=1)

    _normalize_answer = field_validator("answer")(_clean_nonempty)
    _normalize_citations = field_validator("citations")(_clean_citations)


class ClarificationModelAnswer(ClosedModel):
    status: Literal["needs_clarification"]
    answer: str = Field(min_length=1)
    citations: list[str] = Field(min_length=1)
    clarification_questions: list[str] = Field(min_length=1, max_length=3)

    _normalize_answer = field_validator("answer")(_clean_nonempty)
    _normalize_citations = field_validator("citations")(_clean_citations)

    @field_validator("clarification_questions")
    @classmethod
    def normalize_questions(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(_clean_nonempty(value) for value in values))
        if not 1 <= len(cleaned) <= 3:
            raise ValueError("clarification_question_count_invalid")
        if any(len(value) > 200 for value in cleaned):
            raise ValueError("clarification_question_length_invalid")
        if any(_SENSITIVE_QUESTION.search(value) for value in cleaned):
            raise ValueError("clarification_question_sensitive")
        return cleaned


class AbstainedModelAnswer(ClosedModel):
    status: Literal["abstained"]
    answer: None
    citations: list[str] = Field(default_factory=list, max_length=0)


_ModelAnswerPayload = Annotated[
    AnsweredModelAnswer | ClarificationModelAnswer | AbstainedModelAnswer,
    Field(discriminator="status"),
]
_MODEL_ANSWER_ADAPTER = TypeAdapter(_ModelAnswerPayload)


@dataclass(frozen=True, slots=True)
class ModelAnswer:
    status: Literal["answered", "needs_clarification", "abstained"]
    answer: str | None
    citations: tuple[str, ...]
    clarification_questions: tuple[str, ...] = ()


def parse_model_answer(
    payload: object,
    *,
    allowed_citation_ids: set[str],
) -> ModelAnswer:
    try:
        parsed = _MODEL_ANSWER_ADAPTER.validate_python(payload)
        citations = tuple(parsed.citations)
        if not set(citations) <= allowed_citation_ids:
            raise ValueError("citation_not_allowed")
        questions = (
            tuple(parsed.clarification_questions)
            if isinstance(parsed, ClarificationModelAnswer)
            else ()
        )
        return ModelAnswer(parsed.status, parsed.answer, citations, questions)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ModelAnswerError("model_output_invalid") from exc
