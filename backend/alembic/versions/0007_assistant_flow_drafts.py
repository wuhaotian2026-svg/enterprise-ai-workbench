"""Add owner-scoped assistant flow drafts and stable request text."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007_assistant_flow_drafts"
down_revision = "0006_approval_command_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_flow_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("module_key", sa.String(length=80), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intent", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("field_values", sa.JSON(), nullable=False),
        sa.Column("field_sources", sa.JSON(), nullable=False),
        sa.Column("pending_candidates", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_assistant_flow_draft_version_positive"),
        sa.CheckConstraint(
            "status IN ('active', 'closed', 'cleared', 'expired')",
            name="ck_assistant_flow_draft_status",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"],
            name="fk_assistant_flow_drafts_owner_user_id", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "module_key",
            "conversation_id",
            "intent",
            name="uq_assistant_flow_draft_scope_intent",
        ),
    )
    op.create_index(
        "ix_assistant_flow_drafts_scope_status",
        "assistant_flow_drafts",
        ["owner_user_id", "module_key", "conversation_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_assistant_flow_drafts_expiry",
        "assistant_flow_drafts",
        ["status", "expires_at"],
        unique=False,
    )
    op.add_column(
        "assistant_turns",
        sa.Column("request_content", sa.Text(), nullable=True),
    )
    op.execute("UPDATE assistant_turns SET request_content = content")
    op.alter_column("assistant_turns", "request_content", nullable=False)


def downgrade() -> None:
    op.drop_column("assistant_turns", "request_content")
    op.drop_index("ix_assistant_flow_drafts_expiry", table_name="assistant_flow_drafts")
    op.drop_index("ix_assistant_flow_drafts_scope_status", table_name="assistant_flow_drafts")
    op.drop_table("assistant_flow_drafts")
