"""Add the low-sensitivity slot extraction operation ledger."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0008_slot_extraction_operations"
down_revision = "0007_assistant_flow_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_slot_extraction_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("module_key", sa.String(length=20), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("fingerprint_key_id", sa.String(length=80), nullable=False),
        sa.Column("slot_schema_version", sa.String(length=80), nullable=False),
        sa.Column("slot_schema_sha256", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("pending_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "module_key IN ('hr', 'procurement')",
            name="ck_slot_extraction_operation_module",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'dispatched', 'succeeded', 'failed', "
            "'indeterminate')",
            name="ck_slot_extraction_operation_status",
        ),
        sa.CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name="ck_slot_extraction_operation_request_fingerprint",
        ),
        sa.CheckConstraint(
            "char_length(slot_schema_sha256) = 64",
            name="ck_slot_extraction_operation_schema_sha256",
        ),
        sa.CheckConstraint(
            "accepted_count >= 0 AND pending_count >= 0 AND rejected_count >= 0",
            name="ck_slot_extraction_operation_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'indeterminate') "
            "OR completed_at IS NOT NULL",
            name="ck_slot_extraction_operation_terminal_completed",
        ),
        sa.CheckConstraint(
            "status != 'dispatched' OR dispatched_at IS NOT NULL",
            name="ck_slot_extraction_operation_dispatched_at",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_slot_extraction_operations_owner_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "module_key",
            "client_turn_id",
            name="uq_slot_extraction_operation_owner_module_turn",
        ),
    )
    op.create_index(
        "ix_slot_extraction_operations_status_deadline",
        "assistant_slot_extraction_operations",
        ["status", "deadline_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_slot_extraction_operations_status_deadline",
        table_name="assistant_slot_extraction_operations",
    )
    op.drop_table("assistant_slot_extraction_operations")
