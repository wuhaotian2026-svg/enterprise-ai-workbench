"""Add the procurement approval-center persistence snapshot."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_procurement_approval_center"
down_revision = "0004_rag_clarification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_key", sa.String(length=120), nullable=False),
        sa.Column("process_version", sa.Integer(), nullable=False),
        sa.Column("subject_type", sa.String(length=80), nullable=False),
        sa.Column("applicant_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("current_step_key", sa.String(length=120), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'running' AND current_step_key IS NOT NULL AND completed_at IS NULL) "
            "OR (status IN ('approved', 'rejected', 'cancelled') "
            "AND current_step_key IS NULL AND completed_at IS NOT NULL)",
            name="ck_approval_instance_state_shape",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_approval_instance_version_positive"
        ),
        sa.ForeignKeyConstraint(
            ["applicant_user_id"],
            ["users.id"],
            name="fk_approval_instances_applicant_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_unit_id"],
            ["organization_units.id"],
            name="fk_approval_instances_organization_unit_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_approval_instances_applicant_status",
        "approval_instances",
        ["applicant_user_id", "status"],
    )
    op.create_index(
        "ix_approval_instances_organization_status",
        "approval_instances",
        ["organization_unit_id", "status"],
    )
    op.create_index(
        "ix_approval_instances_process_status",
        "approval_instances",
        ["process_key", "process_version", "status"],
    )
    op.create_index(
        "ix_approval_instances_submitted_at",
        "approval_instances",
        ["submitted_at"],
    )

    op.create_table(
        "approval_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("step_key", sa.String(length=120), nullable=False),
        sa.Column("step_label", sa.String(length=160), nullable=False),
        sa.Column("assignment_kind", sa.String(length=10), nullable=False),
        sa.Column("assigned_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("required_capability", sa.String(length=120), nullable=True),
        sa.Column(
            "scope_organization_unit_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(assignment_kind = 'user' AND assigned_user_id IS NOT NULL "
            "AND required_capability IS NULL AND scope_organization_unit_id IS NULL) OR "
            "(assignment_kind = 'capability' AND assigned_user_id IS NULL "
            "AND required_capability IS NOT NULL)",
            name="ck_approval_task_assignment_shape",
        ),
        sa.CheckConstraint(
            "(status = 'waiting' AND activated_at IS NULL AND completed_at IS NULL) OR "
            "(status = 'pending' AND activated_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status IN ('approved', 'rejected') AND activated_at IS NOT NULL "
            "AND completed_at IS NOT NULL) OR "
            "(status = 'cancelled' AND completed_at IS NOT NULL)",
            name="ck_approval_task_state_shape",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_user_id"],
            ["users.id"],
            name="fk_approval_tasks_assigned_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["approval_instances.id"],
            name="fk_approval_tasks_instance_id",
        ),
        sa.ForeignKeyConstraint(
            ["scope_organization_unit_id"],
            ["organization_units.id"],
            name="fk_approval_tasks_scope_organization_unit_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instance_id", "sequence", name="uq_approval_task_instance_sequence"
        ),
        sa.UniqueConstraint(
            "instance_id", "step_key", name="uq_approval_task_instance_step"
        ),
        sa.UniqueConstraint("id", "instance_id", name="uq_approval_task_id_instance"),
    )
    op.create_index(
        "uq_approval_tasks_one_pending_per_instance",
        "approval_tasks",
        ["instance_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_approval_tasks_user_queue",
        "approval_tasks",
        ["status", "assigned_user_id", "activated_at"],
    )
    op.create_index(
        "ix_approval_tasks_capability_scope_queue",
        "approval_tasks",
        [
            "status",
            "required_capability",
            "scope_organization_unit_id",
            "activated_at",
        ],
    )

    op.create_table(
        "approval_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=7), nullable=False),
        sa.Column("comment", sa.String(length=500), nullable=True),
        sa.Column("client_operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action = 'approve' OR (action = 'reject' AND comment IS NOT NULL "
            "AND comment ~ '[^[:space:]]')",
            name="ck_approval_decision_reject_comment",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_approval_decisions_actor_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["approval_instances.id"],
            name="fk_approval_decisions_instance_id",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "instance_id"],
            ["approval_tasks.id", "approval_tasks.instance_id"],
            name="fk_approval_decision_task_instance",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_approval_decision_task"),
        sa.UniqueConstraint(
            "actor_user_id",
            "client_operation_id",
            name="uq_approval_decision_actor_operation",
        ),
    )

    op.create_table(
        "procurement_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_number", sa.String(length=40), nullable=False),
        sa.Column("approval_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("applicant_employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("needed_by_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("total_amount", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "currency = 'CNY'", name="ck_procurement_request_currency_cny"
        ),
        sa.CheckConstraint(
            "total_amount >= 0",
            name="ck_procurement_request_total_amount_nonnegative",
        ),
        sa.CheckConstraint(
            "total_amount <= 999999999999.99",
            name="ck_procurement_request_total_amount_maximum",
        ),
        sa.ForeignKeyConstraint(
            ["applicant_employee_id"],
            ["employee_profiles.id"],
            name="fk_procurement_requests_applicant_employee_id",
        ),
        sa.ForeignKeyConstraint(
            ["approval_instance_id"],
            ["approval_instances.id"],
            name="fk_procurement_requests_approval_instance_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_unit_id"],
            ["organization_units.id"],
            name="fk_procurement_requests_organization_unit_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "approval_instance_id", name="uq_procurement_requests_approval_instance_id"
        ),
        sa.UniqueConstraint("request_number", name="uq_procurement_requests_request_number"),
    )
    op.create_index(
        "ix_procurement_requests_applicant_submitted",
        "procurement_requests",
        ["applicant_employee_id", "submitted_at"],
    )
    op.create_index(
        "ix_procurement_requests_organization_submitted",
        "procurement_requests",
        ["organization_unit_id", "submitted_at"],
    )
    op.create_index(
        "ix_procurement_requests_needed_by_date",
        "procurement_requests",
        ["needed_by_date"],
    )

    op.create_table(
        "procurement_request_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("category_code", sa.String(length=32), nullable=False),
        sa.Column("item_name", sa.String(length=200), nullable=False),
        sa.Column("specification", sa.String(length=500), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("unit", sa.String(length=40), nullable=False),
        sa.Column(
            "estimated_unit_price", sa.Numeric(precision=14, scale=2), nullable=False
        ),
        sa.Column("subtotal", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category_code IN ('office_supplies', 'it_equipment', "
            "'software_service', 'professional_service', 'other')",
            name="ck_procurement_request_item_category_code",
        ),
        sa.CheckConstraint(
            "line_number > 0", name="ck_procurement_item_line_number_positive"
        ),
        sa.CheckConstraint(
            "quantity > 0", name="ck_procurement_item_quantity_positive"
        ),
        sa.CheckConstraint(
            "estimated_unit_price >= 0",
            name="ck_procurement_item_unit_price_nonnegative",
        ),
        sa.CheckConstraint(
            "subtotal >= 0", name="ck_procurement_item_subtotal_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["procurement_requests.id"],
            name="fk_procurement_request_items_request_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id", "line_number", name="uq_procurement_item_request_line"
        ),
    )

    op.create_table(
        "procurement_command_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_kind", sa.String(length=80), nullable=False),
        sa.Column("canonical_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=11), nullable=False),
        sa.Column("result_resource_type", sa.String(length=80), nullable=True),
        sa.Column("result_resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'in_progress' AND result_resource_type IS NULL "
            "AND result_resource_id IS NULL) OR "
            "(status = 'succeeded' AND result_resource_type IS NOT NULL "
            "AND result_resource_id IS NOT NULL)",
            name="ck_procurement_operation_result_shape",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_procurement_command_operations_actor_user_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id",
            "client_operation_id",
            name="uq_procurement_operation_actor_client_operation",
        ),
    )

    op.create_table(
        "assistant_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("module_key", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_assistant_conversations_owner_user_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "owner_user_id",
            "module_key",
            name="uq_assistant_conversation_id_owner_module",
        ),
    )
    op.create_index(
        "ix_assistant_conversations_owner_module_updated",
        "assistant_conversations",
        ["owner_user_id", "module_key", "updated_at"],
    )

    op.create_table(
        "assistant_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("module_key", sa.String(length=80), nullable=False),
        sa.Column("client_turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("blocks", sa.JSON(), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id", "owner_user_id", "module_key"],
            [
                "assistant_conversations.id",
                "assistant_conversations.owner_user_id",
                "assistant_conversations.module_key",
            ],
            name="fk_assistant_turn_conversation_owner_module",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_assistant_turns_owner_user_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "module_key",
            "client_turn_id",
            name="uq_assistant_turn_owner_module_client_turn",
        ),
    )
    op.create_index(
        "ix_assistant_turns_conversation_created",
        "assistant_turns",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("assistant_turns")
    op.drop_table("assistant_conversations")
    op.drop_table("procurement_command_operations")
    op.drop_table("procurement_request_items")
    op.drop_table("procurement_requests")
    op.drop_table("approval_decisions")
    op.drop_table("approval_tasks")
    op.drop_table("approval_instances")
