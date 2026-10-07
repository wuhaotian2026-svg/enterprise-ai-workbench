"""Add HR leave workflow and generic tool audit schema."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_hr_tool_calling"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _identity_columns() -> tuple[sa.Column, sa.Column, sa.Column]:
    return (
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "employee_profiles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_number", sa.String(length=40), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("department", sa.String(length=120), nullable=True),
        sa.Column("hire_date", sa.Date(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_number"),
        sa.UniqueConstraint("user_id", name="uq_employee_profile_user"),
    )
    op.create_table(
        "leave_types",
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        *_identity_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "work_calendar_days",
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("is_workday", sa.Boolean(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        *_identity_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("calendar_date"),
    )
    op.create_table(
        "hr_conversations",
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_hr_conversations_owner_updated",
        "hr_conversations",
        ["owner_user_id", "updated_at"],
    )
    op.create_table(
        "leave_accounts",
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("leave_type_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("entitled", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("used", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("reserved", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        *_identity_columns(),
        sa.CheckConstraint(
            "entitled >= 0", name="ck_leave_account_entitled_nonnegative"
        ),
        sa.CheckConstraint("used >= 0", name="ck_leave_account_used_nonnegative"),
        sa.CheckConstraint(
            "reserved >= 0", name="ck_leave_account_reserved_nonnegative"
        ),
        sa.CheckConstraint(
            "used + reserved <= entitled",
            name="ck_leave_account_within_entitlement",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee_profiles.id"]),
        sa.ForeignKeyConstraint(["leave_type_id"], ["leave_types.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "employee_id",
            "leave_type_id",
            "year",
            name="uq_leave_account_employee_type_year",
        ),
    )
    op.create_index(
        "ix_leave_accounts_employee_year",
        "leave_accounts",
        ["employee_id", "year"],
    )
    op.create_table(
        "leave_requests",
        sa.Column("request_number", sa.String(length=40), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("leave_type_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("workday_count", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_identity_columns(),
        sa.CheckConstraint(
            "end_date >= start_date", name="ck_leave_request_dates_valid"
        ),
        sa.CheckConstraint(
            "workday_count > 0", name="ck_leave_request_workdays_positive"
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND reviewer_user_id IS NULL AND reviewed_at IS NULL "
            "AND rejection_reason IS NULL AND cancelled_at IS NULL "
            "AND cancelled_by_user_id IS NULL) OR "
            "(status = 'approved' AND reviewer_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND rejection_reason IS NULL "
            "AND cancelled_at IS NULL AND cancelled_by_user_id IS NULL) OR "
            "(status = 'rejected' AND reviewer_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND rejection_reason IS NOT NULL "
            "AND cancelled_at IS NULL AND cancelled_by_user_id IS NULL) OR "
            "(status = 'cancelled' AND reviewer_user_id IS NULL "
            "AND reviewed_at IS NULL AND rejection_reason IS NULL "
            "AND cancelled_at IS NOT NULL AND cancelled_by_user_id IS NOT NULL)",
            name="ck_leave_request_state_shape",
        ),
        sa.ForeignKeyConstraint(["cancelled_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["employee_id"], ["employee_profiles.id"]),
        sa.ForeignKeyConstraint(["leave_type_id"], ["leave_types.id"]),
        sa.ForeignKeyConstraint(["reviewer_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_number"),
    )
    op.create_index(
        "ix_leave_requests_employee_status",
        "leave_requests",
        ["employee_id", "status"],
    )
    op.create_index(
        "ix_leave_requests_employee_dates",
        "leave_requests",
        ["employee_id", "start_date", "end_date"],
    )
    op.create_index(
        "ix_leave_requests_review_queue",
        "leave_requests",
        ["status", "submitted_at"],
    )
    op.create_table(
        "leave_account_events",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("leave_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=7), nullable=False),
        sa.Column("amount", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("used_after", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("reserved_after", sa.Numeric(precision=8, scale=2), nullable=False),
        *_identity_columns(),
        sa.CheckConstraint(
            "used_after >= 0", name="ck_leave_event_used_nonnegative"
        ),
        sa.CheckConstraint(
            "reserved_after >= 0", name="ck_leave_event_reserved_nonnegative"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["leave_accounts.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["leave_request_id"], ["leave_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id", name="uq_leave_account_event_operation"
        ),
    )
    op.create_index(
        "ix_leave_account_events_account_created",
        "leave_account_events",
        ["account_id", "created_at"],
    )
    op.create_table(
        "hr_turns",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("blocks", sa.JSON(), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["conversation_id"], ["hr_conversations.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "client_turn_id", name="uq_hr_turn_owner_client_turn"
        ),
    )
    op.create_index(
        "ix_hr_turns_conversation_created",
        "hr_turns",
        ["conversation_id", "created_at"],
    )
    op.create_table(
        "tool_invocations",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_call_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("provider_tool_name", sa.String(length=64), nullable=False),
        sa.Column("risk_level", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("arguments_hash", sa.String(length=64), nullable=False),
        sa.Column("result_resource_type", sa.String(length=80), nullable=True),
        sa.Column("result_resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "provider_call_id",
            name="uq_tool_invocation_conversation_provider_call",
        ),
    )
    op.create_index(
        "ix_tool_invocations_actor_created",
        "tool_invocations",
        ["actor_user_id", "created_at"],
    )
    op.create_index("ix_tool_invocations_turn", "tool_invocations", ["turn_id"])
    op.create_index(
        "ix_tool_invocations_tool_status",
        "tool_invocations",
        ["tool_name", "status"],
    )
    op.create_table(
        "tool_confirmations",
        sa.Column("invocation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("normalized_arguments", sa.JSON(), nullable=False),
        sa.Column("arguments_hash", sa.String(length=64), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_resource_type", sa.String(length=80), nullable=True),
        sa.Column("result_resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_identity_columns(),
        sa.CheckConstraint(
            "(status = 'pending' AND consumed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'consumed' AND consumed_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND consumed_at IS NULL AND cancelled_at IS NOT NULL) OR "
            "(status = 'expired' AND consumed_at IS NULL AND cancelled_at IS NULL)",
            name="ck_tool_confirmation_status_shape",
        ),
        sa.ForeignKeyConstraint(["invocation_id"], ["tool_invocations.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invocation_id", name="uq_tool_confirmation_invocation"),
        sa.UniqueConstraint(
            "owner_user_id",
            "client_operation_id",
            name="uq_tool_confirmation_owner_operation",
        ),
    )
    op.create_index(
        "ix_tool_confirmations_owner_status_expiry",
        "tool_confirmations",
        ["owner_user_id", "status", "expires_at"],
    )
    op.create_table(
        "tool_audit_events",
        sa.Column("invocation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("confirmation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_kind", sa.String(length=21), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["confirmation_id"], ["tool_confirmations.id"]),
        sa.ForeignKeyConstraint(["invocation_id"], ["tool_invocations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tool_audit_invocation_created",
        "tool_audit_events",
        ["invocation_id", "created_at"],
    )
    op.create_index(
        "ix_tool_audit_confirmation_created",
        "tool_audit_events",
        ["confirmation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("tool_audit_events")
    op.drop_table("tool_confirmations")
    op.drop_table("tool_invocations")
    op.drop_table("hr_turns")
    op.drop_table("leave_account_events")
    op.drop_table("leave_requests")
    op.drop_table("leave_accounts")
    op.drop_table("hr_conversations")
    op.drop_table("work_calendar_days")
    op.drop_table("leave_types")
    op.drop_table("employee_profiles")
