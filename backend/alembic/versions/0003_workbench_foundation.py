"""Add organization, capability, audit, and product event foundations."""

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_workbench_foundation"
down_revision = "0002_hr_tool_calling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_units",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name="ck_organization_unit_parent_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["organization_units.id"],
            name="fk_organization_units_parent_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_organization_units_code"),
    )

    op.add_column(
        "employee_profiles",
        sa.Column(
            "organization_unit_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "employee_profiles",
        sa.Column("manager_employee_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_employee_profiles_organization_unit_id",
        "employee_profiles",
        "organization_units",
        ["organization_unit_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_employee_profiles_manager_employee_id",
        "employee_profiles",
        "employee_profiles",
        ["manager_employee_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_employee_profile_manager_not_self",
        "employee_profiles",
        "manager_employee_id IS NULL OR manager_employee_id <> id",
    )

    op.create_table(
        "capability_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability", sa.String(length=80), nullable=False),
        sa.Column("scope_kind", sa.String(length=24), nullable=False),
        sa.Column(
            "organization_unit_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(scope_kind = 'global' AND organization_unit_id IS NULL) OR "
            "(scope_kind = 'unit_subtree' AND organization_unit_id IS NOT NULL)",
            name="ck_capability_grant_scope_shape",
        ),
        sa.ForeignKeyConstraint(
            ["organization_unit_id"],
            ["organization_units.id"],
            name="fk_capability_grants_organization_unit_id",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_capability_grants_user_id"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_capability_grants_active_global",
        "capability_grants",
        ["user_id", "capability"],
        unique=True,
        postgresql_where=sa.text("scope_kind = 'global' AND is_active = true"),
    )
    op.create_index(
        "uq_capability_grants_active_unit_subtree",
        "capability_grants",
        ["user_id", "capability", "organization_unit_id"],
        unique=True,
        postgresql_where=sa.text(
            "scope_kind = 'unit_subtree' AND is_active = true"
        ),
    )

    op.create_table(
        "security_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_name", sa.String(length=80), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_type", sa.String(length=80), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.String(length=80), nullable=False),
        sa.Column("request_id", sa.String(length=120), nullable=True),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_security_audit_events_actor_user_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id",
            "operation_id",
            name="uq_security_audit_actor_operation",
        ),
    )

    op.create_table(
        "product_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_name", sa.String(length=80), nullable=False),
        sa.Column("module_key", sa.String(length=60), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "organization_unit_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("role_snapshot", sa.String(length=24), nullable=False),
        sa.Column("request_id", sa.String(length=120), nullable=True),
        sa.Column("outcome", sa.String(length=80), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "dimensions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_product_event_duration_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_product_events_actor_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_unit_id"],
            ["organization_units.id"],
            name="fk_product_events_organization_unit_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_product_event_event_id"),
    )
    op.create_index(
        "ix_product_events_name_occurred",
        "product_events",
        ["event_name", "occurred_at"],
    )
    op.create_index(
        "ix_product_events_module_occurred",
        "product_events",
        ["module_key", "occurred_at"],
    )
    op.create_index(
        "ix_product_events_actor_occurred",
        "product_events",
        ["actor_user_id", "occurred_at"],
    )
    op.create_index(
        "ix_product_events_organization_occurred",
        "product_events",
        ["organization_unit_id", "occurred_at"],
    )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, role FROM users "
            "WHERE is_active = true AND role IN ('admin', 'hr')"
        )
    ).mappings()
    now = datetime.now(timezone.utc)
    for row in rows:
        capabilities = (
            ("knowledge.manage", "organization.manage", "analytics.view")
            if row["role"] == "admin"
            else ("hr.leave.review",)
        )
        for capability in capabilities:
            connection.execute(
                sa.text(
                    "INSERT INTO capability_grants "
                    "(id, user_id, capability, scope_kind, organization_unit_id, "
                    "is_active, created_at, updated_at) "
                    "VALUES (:id, :user_id, :capability, 'global', NULL, true, "
                    ":now, :now) ON CONFLICT DO NOTHING"
                ),
                {
                    "id": uuid.uuid4(),
                    "user_id": row["id"],
                    "capability": capability,
                    "now": now,
                },
            )


def downgrade() -> None:
    op.drop_table("product_events")
    op.drop_table("security_audit_events")
    op.drop_table("capability_grants")
    op.drop_constraint(
        "ck_employee_profile_manager_not_self",
        "employee_profiles",
        type_="check",
    )
    op.drop_constraint(
        "fk_employee_profiles_manager_employee_id",
        "employee_profiles",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_employee_profiles_organization_unit_id",
        "employee_profiles",
        type_="foreignkey",
    )
    op.drop_column("employee_profiles", "manager_employee_id")
    op.drop_column("employee_profiles", "organization_unit_id")
    op.drop_table("organization_units")
