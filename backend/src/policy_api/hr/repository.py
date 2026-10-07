from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import false, select, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from policy_api.hr.enums import LeaveRequestStatus, LeaveTypeCode
from policy_api.hr.models import (
    EmployeeProfile,
    LeaveAccount,
    LeaveAccountEvent,
    LeaveRequest,
    LeaveType,
    WorkCalendarDay,
)
from policy_api.tools.models import ToolConfirmation
from policy_api.workbench.capabilities import CapabilityScope


def list_owned_leave_accounts(
    db: Session,
    actor_user_id: UUID,
    *,
    year: int | None = None,
) -> list[tuple[LeaveAccount, LeaveType]]:
    statement = (
        select(LeaveAccount, LeaveType)
        .join(EmployeeProfile, EmployeeProfile.id == LeaveAccount.employee_id)
        .join(LeaveType, LeaveType.id == LeaveAccount.leave_type_id)
        .where(EmployeeProfile.user_id == actor_user_id)
        .order_by(LeaveAccount.year.desc(), LeaveType.code)
    )
    if year is not None:
        statement = statement.where(LeaveAccount.year == year)
    return list(db.execute(statement).tuples())


def list_owned_leave_requests(
    db: Session,
    actor_user_id: UUID,
) -> list[tuple[LeaveRequest, LeaveType]]:
    statement = (
        select(LeaveRequest, LeaveType)
        .join(EmployeeProfile, EmployeeProfile.id == LeaveRequest.employee_id)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
        .where(EmployeeProfile.user_id == actor_user_id)
        .order_by(LeaveRequest.submitted_at.desc(), LeaveRequest.id.desc())
    )
    return list(db.execute(statement).tuples())


def find_owned_leave_request(
    db: Session,
    actor_user_id: UUID,
    request_id: UUID,
) -> tuple[LeaveRequest, LeaveType] | None:
    statement = (
        select(LeaveRequest, LeaveType)
        .join(EmployeeProfile, EmployeeProfile.id == LeaveRequest.employee_id)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
        .where(
            EmployeeProfile.user_id == actor_user_id,
            LeaveRequest.id == request_id,
        )
    )
    return db.execute(statement).tuples().one_or_none()


def list_review_requests(
    db: Session,
    *,
    status: LeaveRequestStatus,
    scope: CapabilityScope,
) -> list[tuple[LeaveRequest, LeaveType, EmployeeProfile]]:
    scope_filter = _review_scope_filter(scope)
    statement = (
        select(LeaveRequest, LeaveType, EmployeeProfile)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
        .join(EmployeeProfile, EmployeeProfile.id == LeaveRequest.employee_id)
        .where(LeaveRequest.status == status, scope_filter)
        .order_by(LeaveRequest.submitted_at.asc())
    )
    return list(db.execute(statement).tuples())


def find_review_request(
    db: Session,
    *,
    request_id: UUID,
    scope: CapabilityScope,
    for_update: bool = False,
) -> tuple[LeaveRequest, LeaveType, EmployeeProfile] | None:
    statement = (
        select(LeaveRequest, LeaveType, EmployeeProfile)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
        .join(EmployeeProfile, EmployeeProfile.id == LeaveRequest.employee_id)
        .where(
            LeaveRequest.id == request_id,
            _review_scope_filter(scope),
        )
    )
    if for_update:
        statement = statement.with_for_update(of=LeaveRequest)
    return db.execute(statement).tuples().one_or_none()


def _review_scope_filter(scope: CapabilityScope) -> ColumnElement[bool]:
    if scope.is_global:
        return true()
    if not scope.organization_unit_ids:
        return false()
    return EmployeeProfile.organization_unit_id.in_(scope.organization_unit_ids)


def list_calendar_days(
    db: Session,
    start_date: date,
    end_date: date,
) -> list[WorkCalendarDay]:
    statement = (
        select(WorkCalendarDay)
        .where(
            WorkCalendarDay.calendar_date >= start_date,
            WorkCalendarDay.calendar_date <= end_date,
        )
        .order_by(WorkCalendarDay.calendar_date)
    )
    return list(db.scalars(statement))


def find_operation_event(
    db: Session,
    operation_id: UUID,
) -> LeaveAccountEvent | None:
    return db.scalar(
        select(LeaveAccountEvent).where(
            LeaveAccountEvent.operation_id == operation_id
        )
    )


def lock_employee_by_user(
    db: Session,
    actor_user_id: UUID,
) -> EmployeeProfile | None:
    return db.scalar(
        select(EmployeeProfile)
        .where(EmployeeProfile.user_id == actor_user_id)
        .with_for_update()
    )


def lock_employee_by_id(
    db: Session,
    employee_id: UUID,
) -> EmployeeProfile | None:
    return db.scalar(
        select(EmployeeProfile)
        .where(EmployeeProfile.id == employee_id)
        .with_for_update()
    )


def find_enabled_leave_type(
    db: Session,
    code: LeaveTypeCode,
) -> LeaveType | None:
    return db.scalar(
        select(LeaveType).where(LeaveType.code == code, LeaveType.is_enabled.is_(True))
    )


def lock_leave_account(
    db: Session,
    employee_id: UUID,
    leave_type_id: UUID,
    year: int,
) -> LeaveAccount | None:
    return db.scalar(
        select(LeaveAccount)
        .where(
            LeaveAccount.employee_id == employee_id,
            LeaveAccount.leave_type_id == leave_type_id,
            LeaveAccount.year == year,
        )
        .with_for_update()
    )


def has_active_overlap(
    db: Session,
    employee_id: UUID,
    start_date: date,
    end_date: date,
) -> bool:
    return (
        db.scalar(
            select(LeaveRequest.id).where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.status.in_(
                    [LeaveRequestStatus.PENDING, LeaveRequestStatus.APPROVED]
                ),
                LeaveRequest.start_date <= end_date,
                LeaveRequest.end_date >= start_date,
            )
        )
        is not None
    )


def lock_request(
    db: Session,
    request_id: UUID,
) -> LeaveRequest | None:
    return db.scalar(
        select(LeaveRequest)
        .where(LeaveRequest.id == request_id)
        .with_for_update()
    )


def lock_owned_request(
    db: Session,
    actor_user_id: UUID,
    request_id: UUID,
) -> LeaveRequest | None:
    return db.scalar(
        select(LeaveRequest)
        .join(EmployeeProfile, EmployeeProfile.id == LeaveRequest.employee_id)
        .where(
            EmployeeProfile.user_id == actor_user_id,
            LeaveRequest.id == request_id,
        )
        .with_for_update()
    )


def lock_owned_confirmation(
    db: Session,
    actor_user_id: UUID,
    confirmation_id: UUID,
) -> ToolConfirmation | None:
    return db.scalar(
        select(ToolConfirmation)
        .where(
            ToolConfirmation.id == confirmation_id,
            ToolConfirmation.owner_user_id == actor_user_id,
        )
        .with_for_update()
    )
