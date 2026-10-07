"""Add the idempotent approval command operation ledger."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_approval_command_operations"
down_revision = "0005_procurement_approval_center"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_command_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_kind", sa.String(length=40), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("canonical_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "command_kind IN ('approval.approve', 'approval.reject', 'approval.cancel')",
            name="ck_approval_command_operation_kind",
        ),
        sa.CheckConstraint(
            "(command_kind IN ('approval.approve', 'approval.reject') "
            "AND task_id IS NOT NULL) OR "
            "(command_kind = 'approval.cancel' AND task_id IS NULL)",
            name="ck_approval_command_operation_task_shape",
        ),
        sa.CheckConstraint(
            "char_length(canonical_payload_hash) = 64",
            name="ck_approval_command_operation_payload_hash",
        ),
        sa.CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL) OR "
            "(status = 'succeeded' AND completed_at IS NOT NULL)",
            name="ck_approval_command_operation_status_shape",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_approval_command_operations_actor_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["approval_instances.id"],
            name="fk_approval_command_operations_instance_id",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "instance_id"],
            ["approval_tasks.id", "approval_tasks.instance_id"],
            name="fk_approval_command_operation_task_instance",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id",
            "client_operation_id",
            name="uq_approval_command_operation_actor_client_operation",
        ),
    )


def downgrade() -> None:
    op.drop_table("approval_command_operations")
