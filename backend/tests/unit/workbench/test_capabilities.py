from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityScope,
    OrganizationUnit,
    ScopeKind,
)


def test_capability_catalog_is_closed() -> None:
    assert {item.value for item in Capability} == {
        "knowledge.ask",
        "knowledge.manage",
        "hr.leave.self_service",
        "hr.leave.review",
        "procurement.request.self_service",
        "approval.inbox.view",
        "procurement.department.review",
        "procurement.final.review",
        "organization.manage",
        "analytics.view",
    }


def test_procurement_approval_capability_values_are_stable() -> None:
    assert (
        Capability.PROCUREMENT_REQUEST_SELF_SERVICE.value
        == "procurement.request.self_service"
    )
    assert Capability.APPROVAL_INBOX_VIEW.value == "approval.inbox.view"
    assert (
        Capability.PROCUREMENT_DEPARTMENT_REVIEW.value
        == "procurement.department.review"
    )
    assert (
        Capability.PROCUREMENT_FINAL_REVIEW.value
        == "procurement.final.review"
    )


def test_scope_kind_is_closed() -> None:
    assert {item.value for item in ScopeKind} == {"global", "unit_subtree"}


def test_organization_unit_and_grant_columns_preserve_platform_contract() -> None:
    organization_columns = OrganizationUnit.__table__.columns
    grant_columns = CapabilityGrant.__table__.columns

    assert organization_columns["code"].nullable is False
    assert organization_columns["code"].type.length == 60
    assert organization_columns["name"].nullable is False
    assert organization_columns["parent_id"].nullable is True
    assert grant_columns["user_id"].nullable is False
    assert grant_columns["organization_unit_id"].nullable is True
    assert grant_columns["capability"].type.length == 80
    assert grant_columns["scope_kind"].type.length == 24
    assert "ck_organization_unit_parent_not_self" in {
        constraint.name
        for constraint in OrganizationUnit.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_active_grants_use_shape_check_and_postgresql_partial_unique_indexes() -> None:
    constraints = CapabilityGrant.__table__.constraints
    assert "ck_capability_grant_scope_shape" in {
        constraint.name
        for constraint in constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert not any(isinstance(item, UniqueConstraint) for item in constraints)

    indexes = {
        index.name: index
        for index in CapabilityGrant.__table__.indexes
        if isinstance(index, Index)
    }
    global_index = indexes["uq_capability_grants_active_global"]
    subtree_index = indexes["uq_capability_grants_active_unit_subtree"]
    assert global_index.unique is True
    assert subtree_index.unique is True
    assert tuple(column.name for column in global_index.columns) == (
        "user_id",
        "capability",
    )
    assert tuple(column.name for column in subtree_index.columns) == (
        "user_id",
        "capability",
        "organization_unit_id",
    )
    assert str(global_index.dialect_options["postgresql"]["where"]) == (
        "scope_kind = 'global' AND is_active = true"
    )
    assert str(subtree_index.dialect_options["postgresql"]["where"]) == (
        "scope_kind = 'unit_subtree' AND is_active = true"
    )


def test_capability_scope_is_immutable_and_distinguishes_global_from_units() -> None:
    unit_id = __import__("uuid").uuid4()

    global_scope = CapabilityScope(
        is_global=True,
        organization_unit_ids=frozenset(),
    )
    unit_scope = CapabilityScope(
        is_global=False,
        organization_unit_ids=frozenset({unit_id}),
    )

    assert global_scope.is_global is True
    assert global_scope.organization_unit_ids == frozenset()
    assert unit_scope.is_global is False
    assert unit_scope.organization_unit_ids == frozenset({unit_id})
