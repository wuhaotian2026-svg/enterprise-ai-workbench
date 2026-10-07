"""Add RAG clarification state and indexed Chinese lexical retrieval."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0004_rag_clarification"
down_revision = "0003_workbench_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    pg_trgm_installed = connection.scalar(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"
            ")"
        )
    )
    if not pg_trgm_installed:
        raise RuntimeError("pg_trgm_extension_required")

    op.alter_column(
        "answers",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=24),
        existing_nullable=False,
    )
    op.alter_column(
        "evaluation_cases",
        "expected_status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=24),
        existing_nullable=False,
    )
    op.add_column(
        "answers",
        sa.Column(
            "clarification_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_answers_clarification_payload_shape",
        "answers",
        "(status = 'NEEDS_CLARIFICATION' AND clarification_payload IS NOT NULL) "
        "OR (status <> 'NEEDS_CLARIFICATION' AND clarification_payload IS NULL)",
    )
    op.create_index(
        "ix_document_chunks_text_trgm",
        "document_chunks",
        ["text"],
        postgresql_using="gin",
        postgresql_ops={"text": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_chunks_text_trgm",
        table_name="document_chunks",
        postgresql_using="gin",
    )
    op.drop_constraint(
        "ck_answers_clarification_payload_shape",
        "answers",
        type_="check",
    )
    op.drop_column("answers", "clarification_payload")
    op.alter_column(
        "evaluation_cases",
        "expected_status",
        existing_type=sa.String(length=24),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    op.alter_column(
        "answers",
        "status",
        existing_type=sa.String(length=24),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
