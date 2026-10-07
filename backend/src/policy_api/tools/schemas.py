from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextBlock(StrictBlock):
    type: Literal["text"] = "text"
    text: str


class PolicyCitation(StrictBlock):
    number: int
    chunk_id: str
    document_name: str
    page_number: int | None = None
    evidence_snapshot: str


class PolicyCitationBlock(StrictBlock):
    type: Literal["policy_citations"] = "policy_citations"
    citations: tuple[PolicyCitation, ...]


class PolicyClarificationBlock(StrictBlock):
    type: Literal["policy_clarification"] = "policy_clarification"
    questions: tuple[str, ...] = Field(min_length=1, max_length=3)


class BusinessFact(StrictBlock):
    label: str
    value: object


class BusinessFactsBlock(StrictBlock):
    type: Literal["business_facts"] = "business_facts"
    facts: tuple[BusinessFact, ...]
    queried_at: datetime


class ClarificationBlock(StrictBlock):
    type: Literal["clarification"] = "clarification"
    missing_fields: tuple[str, ...]
    suggestions: tuple[str, ...]


class AssistantDraftBlock(StrictBlock):
    type: Literal["assistant_draft"] = "assistant_draft"
    module_key: Literal["hr", "procurement"]
    intent: str
    status: Literal["active", "closed", "cleared", "expired"]
    version: int = Field(ge=1)
    fields: dict[str, object]
    pending_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]


class ConfirmationBlock(StrictBlock):
    type: Literal["confirmation"] = "confirmation"
    confirmation_id: UUID
    tool_name: str
    preview: dict[str, object]
    expires_at: datetime


class ExecutionResultBlock(StrictBlock):
    type: Literal["execution_result"] = "execution_result"
    resource_type: str
    resource_id: UUID
    result: dict[str, object]


class ErrorBlock(StrictBlock):
    type: Literal["error"] = "error"
    code: str
    retryable: bool = False


TurnBlock: TypeAlias = (
    TextBlock
    | PolicyCitationBlock
    | PolicyClarificationBlock
    | BusinessFactsBlock
    | ClarificationBlock
    | AssistantDraftBlock
    | ConfirmationBlock
    | ExecutionResultBlock
    | ErrorBlock
)


class ToolTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocks: tuple[TurnBlock, ...]
    model_calls: int = Field(ge=0, le=3)
    read_calls: int = Field(ge=0, le=4)
    write_proposals: int = Field(ge=0, le=1)
    slot_extraction_calls: int = Field(default=0, ge=0, le=1)


BLOCK_MODELS = {
    "text": TextBlock,
    "policy_citations": PolicyCitationBlock,
    "policy_clarification": PolicyClarificationBlock,
    "business_facts": BusinessFactsBlock,
    "clarification": ClarificationBlock,
    "assistant_draft": AssistantDraftBlock,
    "confirmation": ConfirmationBlock,
    "execution_result": ExecutionResultBlock,
    "error": ErrorBlock,
}


def parse_turn_block(payload: object) -> TurnBlock:
    if not isinstance(payload, dict):
        raise ValueError("tool_block_invalid")
    block_type = payload.get("type")
    model = BLOCK_MODELS.get(block_type)
    if model is None:
        raise ValueError("tool_block_invalid")
    return model.model_validate(payload)
