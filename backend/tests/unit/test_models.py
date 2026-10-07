from __future__ import annotations

from enum import Enum

from sqlalchemy import UniqueConstraint

from policy_api.models import (
    Answer,
    AnswerCitation,
    AnswerStatus,
    Base,
    Document,
    DocumentStatus,
    Feedback,
    IngestionRun,
    IngestionStage,
    IngestionStatus,
    RefusalReason,
    User,
    UserRole,
)


def enum_values(enum_type: type[Enum]) -> set[str]:
    return {item.value for item in enum_type}


def unique_column_sets(model: type[Base]) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_domain_enums_are_restricted_to_supported_values() -> None:
    assert enum_values(UserRole) == {"employee", "hr", "admin"}
    assert enum_values(DocumentStatus) == {
        "pending",
        "processing",
        "enabled",
        "disabled",
        "parse_failed",
        "index_failed",
    }
    assert enum_values(IngestionStage) == {
        "validating",
        "parsing",
        "chunking",
        "embedding",
        "persisting",
        "completed",
        "failed",
    }
    assert enum_values(IngestionStatus) == {"queued", "running", "completed", "failed"}
    assert enum_values(AnswerStatus) == {
        "answered",
        "needs_clarification",
        "abstained",
        "failed",
    }
    assert enum_values(RefusalReason) == {
        "no_evidence",
        "low_confidence",
        "conflicting_evidence",
        "citation_validation_failed",
        "corpus_unavailable",
    }


def test_document_hash_and_feedback_owner_are_unique() -> None:
    assert ("sha256",) in unique_column_sets(Document)
    assert ("answer_id", "user_id") in unique_column_sets(Feedback)


def test_answer_citation_preserves_chunk_reference_and_snapshot() -> None:
    columns = AnswerCitation.__table__.columns

    assert columns["chunk_id"].nullable is False
    assert columns["evidence_snapshot"].nullable is False
    assert columns["citation_number"].nullable is False


def test_core_tables_use_uuid_primary_keys() -> None:
    for model in (User, Document, IngestionRun, Answer, AnswerCitation, Feedback):
        primary_keys = list(model.__table__.primary_key.columns)
        assert len(primary_keys) == 1
        assert primary_keys[0].name == "id"
        assert primary_keys[0].type.python_type.__name__ == "UUID"
