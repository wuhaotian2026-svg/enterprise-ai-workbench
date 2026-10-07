from __future__ import annotations

from enum import Enum

from sqlalchemy import CheckConstraint, Numeric, UniqueConstraint

from policy_api.hr.enums import (
    LeaveAccountEventKind,
    LeaveRequestStatus,
    LeaveTypeCode,
    WorkCalendarDayKind,
)
from policy_api.hr.models import (
    EmployeeProfile,
    HrConversation,
    HrTurn,
    LeaveAccount,
    LeaveAccountEvent,
    LeaveRequest,
    LeaveType,
    WorkCalendarDay,
)
from policy_api.models import Base, UserRole


def enum_values(enum_type: type[Enum]) -> set[str]:
    return {item.value for item in enum_type}


def unique_column_sets(model: type[Base]) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def check_names(model: type[Base]) -> set[str | None]:
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_hr_role_and_leave_enums_are_closed_to_the_approved_values() -> None:
    assert enum_values(UserRole) == {"employee", "hr", "admin"}
    assert enum_values(LeaveTypeCode) == {"annual", "compensatory"}
    assert enum_values(LeaveRequestStatus) == {
        "pending",
        "approved",
        "rejected",
        "cancelled",
    }
    assert enum_values(LeaveAccountEventKind) == {
        "grant",
        "reserve",
        "release",
        "consume",
        "adjust",
    }
    assert enum_values(WorkCalendarDayKind) == {
        "workday",
        "weekend",
        "holiday",
        "adjusted_workday",
    }


def test_hr_business_and_conversation_tables_join_the_shared_metadata() -> None:
    assert {
        "employee_profiles",
        "leave_types",
        "leave_accounts",
        "leave_account_events",
        "work_calendar_days",
        "leave_requests",
        "hr_conversations",
        "hr_turns",
    } <= set(Base.metadata.tables)


def test_employee_accounts_calendar_and_turn_ids_have_stable_uniqueness() -> None:
    assert ("user_id",) in unique_column_sets(EmployeeProfile)
    assert ("employee_number",) in unique_column_sets(EmployeeProfile)
    assert ("code",) in unique_column_sets(LeaveType)
    assert ("employee_id", "leave_type_id", "year") in unique_column_sets(LeaveAccount)
    assert ("calendar_date",) in unique_column_sets(WorkCalendarDay)
    assert ("request_number",) in unique_column_sets(LeaveRequest)
    assert ("owner_user_id", "client_turn_id") in unique_column_sets(HrTurn)


def test_employee_profile_keeps_legacy_department_and_adds_nullable_links() -> None:
    columns = EmployeeProfile.__table__.columns
    assert columns["department"].nullable is True
    assert columns["organization_unit_id"].nullable is True
    assert columns["manager_employee_id"].nullable is True
    assert "ck_employee_profile_manager_not_self" in check_names(EmployeeProfile)


def test_leave_account_uses_exact_days_and_named_balance_constraints() -> None:
    for column_name in ("entitled", "used", "reserved"):
        column_type = LeaveAccount.__table__.columns[column_name].type
        assert isinstance(column_type, Numeric)
        assert column_type.precision == 8
        assert column_type.scale == 2
    assert {
        "ck_leave_account_entitled_nonnegative",
        "ck_leave_account_used_nonnegative",
        "ck_leave_account_reserved_nonnegative",
        "ck_leave_account_within_entitlement",
    } <= check_names(LeaveAccount)


def test_leave_request_has_date_workday_and_state_shape_constraints() -> None:
    assert {
        "ck_leave_request_dates_valid",
        "ck_leave_request_workdays_positive",
        "ck_leave_request_state_shape",
    } <= check_names(LeaveRequest)
    assert LeaveRequest.__table__.columns["reason"].nullable is False
    assert LeaveRequest.__table__.columns["reviewer_user_id"].nullable is True
    assert LeaveRequest.__table__.columns["cancelled_by_user_id"].nullable is True


def test_balance_events_are_linked_to_account_request_actor_and_operation() -> None:
    columns = LeaveAccountEvent.__table__.columns
    assert columns["account_id"].nullable is False
    assert columns["leave_request_id"].nullable is True
    assert columns["actor_user_id"].nullable is False
    assert columns["operation_id"].nullable is False
    assert ("operation_id",) in unique_column_sets(LeaveAccountEvent)
