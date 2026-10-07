from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StringEnum(str, enum.Enum):
    pass


class UserRole(StringEnum):
    EMPLOYEE = "employee"
    HR = "hr"
    ADMIN = "admin"


class UserRoleStorage(TypeDecorator[UserRole]):
    """Persist canonical role values while reading legacy enum names."""

    impl = String(8)
    cache_ok = True

    def process_bind_param(self, value: UserRole | str | None, dialect) -> str | None:
        del dialect
        if value is None:
            return None
        if isinstance(value, UserRole):
            return value.value
        try:
            return UserRole(value).value
        except ValueError:
            try:
                return UserRole[value].value
            except KeyError as exc:
                raise ValueError("invalid_user_role") from exc

    def process_result_value(self, value: str | None, dialect) -> UserRole | None:
        del dialect
        if value is None:
            return None
        try:
            return UserRole(value)
        except ValueError:
            try:
                return UserRole[value]
            except KeyError as exc:
                raise LookupError("invalid_user_role_storage_value") from exc


class DocumentStatus(StringEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    ENABLED = "enabled"
    DISABLED = "disabled"
    PARSE_FAILED = "parse_failed"
    INDEX_FAILED = "index_failed"


class IngestionStage(StringEnum):
    VALIDATING = "validating"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    PERSISTING = "persisting"
    COMPLETED = "completed"
    FAILED = "failed"


class IngestionStatus(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AnswerStatus(StringEnum):
    ANSWERED = "answered"
    NEEDS_CLARIFICATION = "needs_clarification"
    ABSTAINED = "abstained"
    FAILED = "failed"


class RefusalReason(StringEnum):
    NO_EVIDENCE = "no_evidence"
    LOW_CONFIDENCE = "low_confidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    CITATION_VALIDATION_FAILED = "citation_validation_failed"
    CORPUS_UNAVAILABLE = "corpus_unavailable"


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(UserRoleStorage(), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Session(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sessions"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(Enum(DocumentStatus, native_enum=False), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    active_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class IngestionRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ingestion_runs"
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    stage: Mapped[IngestionStage] = mapped_column(Enum(IngestionStage, native_enum=False), nullable=False)
    status: Mapped[IngestionStatus] = mapped_column(Enum(IngestionStatus, native_enum=False), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("ingestion_run_id", "sequence", name="uq_chunk_run_sequence"),)
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_runs.id"), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str | None] = mapped_column(String(500))
    page_number: Mapped[int | None] = mapped_column(Integer)
    location: Mapped[str | None] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector())
    search_vector: Mapped[object | None] = mapped_column(TSVECTOR)


class Question(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "questions"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)


class Answer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "answers"
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id"), nullable=False, unique=True)
    status: Mapped[AnswerStatus] = mapped_column(Enum(AnswerStatus, native_enum=False), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    refusal_reason: Mapped[RefusalReason | None] = mapped_column(Enum(RefusalReason, native_enum=False))
    evidence_score: Mapped[float | None] = mapped_column(Float)
    model_name: Mapped[str | None] = mapped_column(String(120))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    clarification_payload: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True)
    )


class AnswerCitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "answer_citations"
    __table_args__ = (UniqueConstraint("answer_id", "citation_number", name="uq_answer_citation_number"),)
    answer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("answers.id"), nullable=False, index=True)
    chunk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_chunks.id"), nullable=False)
    citation_number: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_snapshot: Mapped[str] = mapped_column(Text, nullable=False)


class Feedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("answer_id", "user_id", name="uq_feedback_answer_user"),)
    answer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("answers.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    is_helpful: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))


class EvaluationCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_cases"
    external_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected_status: Mapped[AnswerStatus] = mapped_column(Enum(AnswerStatus, native_enum=False), nullable=False)
    expected_facts: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    expected_sources: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)


class EvaluationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_runs"
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    configuration: Mapped[dict] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (UniqueConstraint("evaluation_run_id", "evaluation_case_id", name="uq_eval_result_run_case"),)
    evaluation_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_runs.id"), nullable=False)
    evaluation_case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_cases.id"), nullable=False)
    answer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("answers.id"))
    passed: Mapped[bool | None] = mapped_column(Boolean)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
