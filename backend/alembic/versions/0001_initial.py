"""Create the frozen initial Policy schema."""

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def _identity_columns() -> tuple[sa.Column, sa.Column, sa.Column]:
    return (
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("username", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=8), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_identity_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "sessions",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "documents",
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("active_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_identity_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingestion_runs_document_id", "ingestion_runs", ["document_id"])
    op.create_table(
        "document_chunks",
        sa.Column("ingestion_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.String(length=500), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ingestion_run_id", "sequence", name="uq_chunk_run_sequence"),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])
    op.create_index(
        "ix_document_chunks_ingestion_run_id",
        "document_chunks",
        ["ingestion_run_id"],
    )
    op.create_index("ix_document_chunks_text_hash", "document_chunks", ["text_hash"])
    op.create_index(
        "ix_chunks_search_vector",
        "document_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )

    op.create_table(
        "questions",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_questions_user_id", "questions", ["user_id"])
    op.create_table(
        "answers",
        sa.Column("question_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("refusal_reason", sa.String(length=26), nullable=True),
        sa.Column("evidence_score", sa.Float(), nullable=True),
        sa.Column("model_name", sa.String(length=120), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("question_id"),
    )
    op.create_table(
        "answer_citations",
        sa.Column("answer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("citation_number", sa.Integer(), nullable=False),
        sa.Column("evidence_snapshot", sa.Text(), nullable=False),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"]),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("answer_id", "citation_number", name="uq_answer_citation_number"),
    )
    op.create_index("ix_answer_citations_answer_id", "answer_citations", ["answer_id"])
    op.create_table(
        "feedback",
        sa.Column("answer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_helpful", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("answer_id", "user_id", name="uq_feedback_answer_user"),
    )

    op.create_table(
        "evaluation_cases",
        sa.Column("external_id", sa.String(length=120), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expected_status", sa.String(length=9), nullable=False),
        sa.Column("expected_facts", sa.JSON(), nullable=False),
        sa.Column("expected_sources", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        *_identity_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_identity_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "evaluation_results",
        sa.Column("evaluation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evaluation_case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("answer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"]),
        sa.ForeignKeyConstraint(["evaluation_case_id"], ["evaluation_cases.id"]),
        sa.ForeignKeyConstraint(["evaluation_run_id"], ["evaluation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evaluation_run_id",
            "evaluation_case_id",
            name="uq_eval_result_run_case",
        ),
    )


def downgrade() -> None:
    op.drop_table("evaluation_results")
    op.drop_table("evaluation_runs")
    op.drop_table("evaluation_cases")
    op.drop_table("feedback")
    op.drop_table("answer_citations")
    op.drop_table("answers")
    op.drop_table("questions")
    op.drop_table("document_chunks")
    op.drop_table("ingestion_runs")
    op.drop_table("documents")
    op.drop_table("sessions")
    op.drop_table("users")
