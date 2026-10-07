from __future__ import annotations

from enum import Enum

import pytest
from sqlalchemy import (
    CheckConstraint,
    Enum as SqlAlchemyEnum,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)

from policy_api.models import Base
from policy_api.procurement import enums as procurement_enums
from policy_api.procurement.models import (
    AssistantConversation,
    AssistantTurn,
    ProcurementCommandOperation,
    ProcurementRequest,
    ProcurementRequestItem,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.sqltypes import NullType


def enum_values(enum_type: type[Enum]) -> set[str]:
    return {item.value for item in enum_type}


def unique_column_sets(model: type[Base]) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def check_sql(model: type[Base], name: str) -> str:
    constraint = next(
        constraint
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == name
    )
    return " ".join(str(constraint.sqltext).split())


def index_by_name(model: type[Base], name: str) -> Index:
    return next(index for index in model.__table__.indexes if index.name == name)


def foreign_key_targets(model: type[Base], column_name: str) -> set[str]:
    return {
        foreign_key.target_fullname
        for foreign_key in model.__table__.columns[column_name].foreign_keys
    }


def numeric_shape(model: type[Base], column_name: str) -> tuple[int | None, int | None]:
    column_type = model.__table__.columns[column_name].type
    assert isinstance(column_type, Numeric)
    return column_type.precision, column_type.scale


def test_procurement_enums_are_closed_to_frozen_catalog_currency_and_operation_values() -> None:
    assert hasattr(procurement_enums, "ProcurementCategoryCode")
    assert hasattr(procurement_enums, "ProcurementCurrencyCode")
    assert not hasattr(procurement_enums, "ProcurementCommandKind")
    assert enum_values(procurement_enums.ProcurementCategoryCode) == {
        "office_supplies",
        "it_equipment",
        "software_service",
        "professional_service",
        "other",
    }
    assert enum_values(procurement_enums.ProcurementCurrencyCode) == {"CNY"}
    assert enum_values(procurement_enums.ProcurementCommandOperationStatus) == {
        "in_progress",
        "succeeded",
    }
    category_type = ProcurementRequestItem.__table__.columns["category_code"].type
    assert isinstance(category_type, SqlAlchemyEnum)
    assert category_type.native_enum is False
    assert category_type.create_constraint is False
    assert category_type.validate_strings is True
    assert category_type.name == "ck_procurement_request_item_category_code"
    assert category_type.enums == [
        "office_supplies",
        "it_equipment",
        "software_service",
        "professional_service",
        "other",
    ]
    processor = category_type.bind_processor(postgresql.dialect())
    assert processor is not None
    assert processor("office_supplies") == "office_supplies"
    with pytest.raises(LookupError):
        processor("unapproved_category")
    assert check_sql(
        ProcurementRequestItem, "ck_procurement_request_item_category_code"
    ) == (
        "category_code IN ('office_supplies', 'it_equipment', 'software_service', "
        "'professional_service', 'other')"
    )


def test_procurement_tables_join_shared_metadata() -> None:
    assert {
        "procurement_requests",
        "procurement_request_items",
        "procurement_command_operations",
        "assistant_conversations",
        "assistant_turns",
    } <= set(Base.metadata.tables)


def test_procurement_operation_is_actor_scoped_and_locks_result_shape() -> None:
    columns = ProcurementCommandOperation.__table__.columns
    for column_name in (
        "actor_user_id",
        "client_operation_id",
        "command_kind",
        "canonical_payload_hash",
        "status",
        "created_at",
        "updated_at",
    ):
        assert columns[column_name].nullable is False
    assert type(columns["command_kind"].type) is String
    assert columns["command_kind"].type.length == 80
    assert columns["canonical_payload_hash"].type.length == 64
    assert columns["result_resource_type"].nullable is True
    assert columns["result_resource_id"].nullable is True
    assert foreign_key_targets(ProcurementCommandOperation, "actor_user_id") == {"users.id"}
    assert ("actor_user_id", "client_operation_id") in unique_column_sets(
        ProcurementCommandOperation
    )
    assert check_sql(ProcurementCommandOperation, "ck_procurement_operation_result_shape") == (
        "(status = 'in_progress' AND result_resource_type IS NULL "
        "AND result_resource_id IS NULL) OR "
        "(status = 'succeeded' AND result_resource_type IS NOT NULL "
        "AND result_resource_id IS NOT NULL)"
    )


def test_request_has_one_approval_and_only_cny_denominated_totals() -> None:
    columns = ProcurementRequest.__table__.columns
    assert "status" not in columns
    for column_name in (
        "request_number",
        "approval_instance_id",
        "applicant_employee_id",
        "organization_unit_id",
        "title",
        "purpose",
        "needed_by_date",
        "currency",
        "total_amount",
        "submitted_at",
    ):
        assert columns[column_name].nullable is False
    assert columns["request_number"].type.length == 40
    assert columns["title"].type.length == 160
    assert columns["currency"].type.length == 3
    assert foreign_key_targets(ProcurementRequest, "approval_instance_id") == {
        "approval_instances.id"
    }
    assert foreign_key_targets(ProcurementRequest, "applicant_employee_id") == {
        "employee_profiles.id"
    }
    assert foreign_key_targets(ProcurementRequest, "organization_unit_id") == {
        "organization_units.id"
    }
    assert {
        ("request_number",),
        ("approval_instance_id",),
    } <= unique_column_sets(ProcurementRequest)
    assert numeric_shape(ProcurementRequest, "total_amount") == (16, 2)
    assert check_sql(ProcurementRequest, "ck_procurement_request_currency_cny") == "currency = 'CNY'"
    assert check_sql(
        ProcurementRequest, "ck_procurement_request_total_amount_nonnegative"
    ) == "total_amount >= 0"
    assert check_sql(
        ProcurementRequest, "ck_procurement_request_total_amount_maximum"
    ) == "total_amount <= 999999999999.99"
    assert tuple(
        column.name
        for column in index_by_name(
            ProcurementRequest, "ix_procurement_requests_applicant_submitted"
        ).columns
    ) == ("applicant_employee_id", "submitted_at")
    assert tuple(
        column.name
        for column in index_by_name(
            ProcurementRequest, "ix_procurement_requests_organization_submitted"
        ).columns
    ) == ("organization_unit_id", "submitted_at")
    assert tuple(
        column.name
        for column in index_by_name(
            ProcurementRequest, "ix_procurement_requests_needed_by_date"
        ).columns
    ) == ("needed_by_date",)


def test_request_item_uses_frozen_names_lengths_numbers_and_guards() -> None:
    columns = ProcurementRequestItem.__table__.columns
    assert "category" not in columns
    assert "name" not in columns
    for column_name in (
        "request_id",
        "line_number",
        "category_code",
        "item_name",
        "quantity",
        "unit",
        "estimated_unit_price",
        "subtotal",
    ):
        assert columns[column_name].nullable is False
    assert columns["category_code"].type.length == 32
    assert columns["item_name"].type.length == 200
    assert columns["specification"].nullable is True
    assert columns["specification"].type.length == 500
    assert columns["unit"].type.length == 40
    assert foreign_key_targets(ProcurementRequestItem, "request_id") == {
        "procurement_requests.id"
    }
    assert ("request_id", "line_number") in unique_column_sets(ProcurementRequestItem)
    assert numeric_shape(ProcurementRequestItem, "quantity") == (12, 2)
    assert numeric_shape(ProcurementRequestItem, "estimated_unit_price") == (14, 2)
    assert numeric_shape(ProcurementRequestItem, "subtotal") == (16, 2)
    assert check_sql(ProcurementRequestItem, "ck_procurement_item_line_number_positive") == "line_number > 0"
    assert check_sql(ProcurementRequestItem, "ck_procurement_item_quantity_positive") == "quantity > 0"
    assert check_sql(
        ProcurementRequestItem, "ck_procurement_item_unit_price_nonnegative"
    ) == "estimated_unit_price >= 0"
    assert check_sql(ProcurementRequestItem, "ck_procurement_item_subtotal_nonnegative") == "subtotal >= 0"


def test_assistant_turn_is_scoped_to_conversation_owner_and_module() -> None:
    conversation_columns = AssistantConversation.__table__.columns
    assert "module" not in conversation_columns
    for column_name in ("owner_user_id", "module_key", "title", "is_archived"):
        assert conversation_columns[column_name].nullable is False
    assert conversation_columns["module_key"].type.length == 80
    assert foreign_key_targets(AssistantConversation, "owner_user_id") == {"users.id"}
    assert (
        "id",
        "owner_user_id",
        "module_key",
    ) in unique_column_sets(AssistantConversation)
    assert tuple(
        column.name
        for column in index_by_name(
            AssistantConversation, "ix_assistant_conversations_owner_module_updated"
        ).columns
    ) == ("owner_user_id", "module_key", "updated_at")

    turn_columns = AssistantTurn.__table__.columns
    assert "module" not in turn_columns
    for column_name in (
        "conversation_id",
        "owner_user_id",
        "module_key",
        "client_turn_id",
        "role",
        "content",
        "blocks",
    ):
        assert turn_columns[column_name].nullable is False
    assert turn_columns["module_key"].type.length == 80
    assert ("owner_user_id", "module_key", "client_turn_id") in unique_column_sets(
        AssistantTurn
    )
    composite_link = next(
        constraint
        for constraint in AssistantTurn.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and tuple(column.name for column in constraint.columns)
        == ("conversation_id", "owner_user_id", "module_key")
    )
    assert tuple(element.target_fullname for element in composite_link.elements) == (
        "assistant_conversations.id",
        "assistant_conversations.owner_user_id",
        "assistant_conversations.module_key",
    )
    assert tuple(
        column.name
        for column in index_by_name(
            AssistantTurn, "ix_assistant_turns_conversation_created"
        ).columns
    ) == ("conversation_id", "created_at")


def test_procurement_foreign_key_columns_have_explicit_uuid_types() -> None:
    for model in (
        ProcurementRequest,
        ProcurementRequestItem,
        ProcurementCommandOperation,
        AssistantConversation,
        AssistantTurn,
    ):
        for column in model.__table__.columns:
            if column.foreign_keys:
                assert not isinstance(column.type, NullType), f"{model.__name__}.{column.name}"
