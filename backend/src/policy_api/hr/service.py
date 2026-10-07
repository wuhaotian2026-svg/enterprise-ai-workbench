from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from policy_api.hr.calendar import calculate_leave_duration
from policy_api.hr.enums import (
    LeaveAccountEventKind,
    LeaveRequestStatus,
    LeaveTypeCode,
)
from policy_api.hr.models import (
    LeaveAccount,
    LeaveAccountEvent,
    LeaveRequest,
    LeaveType,
)
from policy_api.hr.repository import (
    find_owned_leave_request,
    find_review_request,
    find_enabled_leave_type,
    find_operation_event,
    has_active_overlap,
    list_calendar_days,
    list_owned_leave_accounts,
    list_owned_leave_requests,
    lock_employee_by_id,
    lock_employee_by_user,
    lock_leave_account,
    lock_owned_confirmation,
    lock_owned_request,
    lock_request,
)
from policy_api.hr.schemas import (
    HrDomainError,
    LeaveBalanceView,
    LeaveDuration,
    LeaveRequestView,
)
from policy_api.tools.enums import ToolConfirmationStatus
from policy_api.tools.models import ToolConfirmation
from policy_api.models import User
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityResolver,
    CapabilityScope,
)


def get_my_leave_balances(
    db: Session,
    actor_user_id: UUID,
    *,
    year: int | None = None,
) -> tuple[LeaveBalanceView, ...]:
    return tuple(
        LeaveBalanceView(
            account_id=account.id,
            leave_type_code=leave_type.code,
            leave_type_name=leave_type.display_name,
            year=account.year,
            entitled=account.entitled,
            used=account.used,
            reserved=account.reserved,
            available=account.entitled - account.used - account.reserved,
        )
        for account, leave_type in list_owned_leave_accounts(
            db, actor_user_id, year=year
        )
    )


def list_my_leave_requests(
    db: Session,
    actor_user_id: UUID,
) -> tuple[LeaveRequestView, ...]:
    return tuple(
        _request_view(request, leave_type)
        for request, leave_type in list_owned_leave_requests(db, actor_user_id)
    )


def get_my_leave_request(
    db: Session,
    actor_user_id: UUID,
    request_id: UUID,
) -> LeaveRequestView:
    result = find_owned_leave_request(db, actor_user_id, request_id)
    if result is None:
        raise HrDomainError("leave_request_not_found")
    return _request_view(*result)


def get_leave_duration(
    db: Session,
    start_date: date,
    end_date: date,
) -> LeaveDuration:
    return calculate_leave_duration(
        start_date,
        end_date,
        list_calendar_days(db, start_date, end_date),
    )


def submit_leave_request(
    db: Session,
    actor_user_id: UUID,
    confirmation_id: UUID,
    client_operation_id: UUID,
    *,
    after_mutation: Callable[[LeaveRequest], None] | None = None,
    commit: bool = True,
) -> LeaveRequestView:
    def operation() -> LeaveRequest:
        confirmation = lock_owned_confirmation(
            db, actor_user_id, confirmation_id
        )
        if confirmation is None:
            raise HrDomainError("confirmation_not_found")
        if confirmation.status == ToolConfirmationStatus.CONSUMED:
            if (
                confirmation.client_operation_id == client_operation_id
                and confirmation.result_resource_id is not None
            ):
                replay = lock_owned_request(
                    db, actor_user_id, confirmation.result_resource_id
                )
                if replay is not None:
                    return replay
            raise HrDomainError("confirmation_already_used")
        _require_pending_confirmation(confirmation, "hr.submit_leave_request")
        if find_operation_event(db, client_operation_id) is not None:
            raise HrDomainError("operation_id_conflict")

        leave_type_code, start_date, end_date, reason = _submit_arguments(
            confirmation.normalized_arguments
        )
        if start_date.year != end_date.year:
            raise HrDomainError("leave_date_range_invalid")
        employee = lock_employee_by_user(db, actor_user_id)
        if employee is None or not employee.is_active:
            raise HrDomainError("employee_profile_not_found")
        leave_type = find_enabled_leave_type(db, leave_type_code)
        if leave_type is None:
            raise HrDomainError("leave_type_not_enabled")
        account = lock_leave_account(
            db, employee.id, leave_type.id, start_date.year
        )
        if account is None:
            raise HrDomainError("leave_account_not_found")
        duration = get_leave_duration(db, start_date, end_date)
        if account.entitled - account.used - account.reserved < duration.workday_count:
            raise HrDomainError("leave_balance_insufficient")
        if has_active_overlap(db, employee.id, start_date, end_date):
            raise HrDomainError("leave_request_overlap")

        request = LeaveRequest(
            request_number=f"LR-{uuid4().hex.upper()}",
            employee_id=employee.id,
            leave_type_id=leave_type.id,
            start_date=start_date,
            end_date=end_date,
            workday_count=duration.workday_count,
            reason=reason,
            status=LeaveRequestStatus.PENDING,
            submitted_at=datetime.now(timezone.utc),
        )
        db.add(request)
        db.flush()
        account.reserved += duration.workday_count
        account.version += 1
        db.add(
            _balance_event(
                account=account,
                request=request,
                actor_user_id=actor_user_id,
                operation_id=client_operation_id,
                kind=LeaveAccountEventKind.RESERVE,
                amount=duration.workday_count,
            )
        )
        confirmation.status = ToolConfirmationStatus.CONSUMED
        confirmation.client_operation_id = client_operation_id
        confirmation.consumed_at = datetime.now(timezone.utc)
        confirmation.result_resource_type = "leave_request"
        confirmation.result_resource_id = request.id
        return request

    return _run_write(db, operation, after_mutation=after_mutation, commit=commit)


def approve_leave_request(
    db: Session,
    reviewer_user_id: UUID,
    request_id: UUID,
    client_operation_id: UUID,
    *,
    review_scope: CapabilityScope,
    capability_resolver: CapabilityResolver,
    after_mutation: Callable[[LeaveRequest], None] | None = None,
    commit: bool = True,
) -> LeaveRequestView:
    _require_request_in_review_scope(db, request_id, review_scope)
    replay = _replay_transition(
        db,
        actor_user_id=reviewer_user_id,
        request_id=request_id,
        operation_id=client_operation_id,
        event_kind=LeaveAccountEventKind.CONSUME,
        final_status=LeaveRequestStatus.APPROVED,
    )
    if replay is not None:
        if commit: db.commit()
        return replay

    def operation() -> LeaveRequest:
        request, account = _lock_scoped_pending_request_and_account(
            db,
            reviewer_user_id=reviewer_user_id,
            request_id=request_id,
            review_scope=review_scope,
            capability_resolver=capability_resolver,
        )
        account.reserved -= request.workday_count
        account.used += request.workday_count
        account.version += 1
        request.status = LeaveRequestStatus.APPROVED
        request.reviewer_user_id = reviewer_user_id
        request.reviewed_at = datetime.now(timezone.utc)
        db.add(
            _balance_event(
                account=account,
                request=request,
                actor_user_id=reviewer_user_id,
                operation_id=client_operation_id,
                kind=LeaveAccountEventKind.CONSUME,
                amount=request.workday_count,
            )
        )
        return request

    return _run_write(db, operation, after_mutation=after_mutation, commit=commit)


def reject_leave_request(
    db: Session,
    reviewer_user_id: UUID,
    request_id: UUID,
    client_operation_id: UUID,
    reason: str,
    *,
    review_scope: CapabilityScope,
    capability_resolver: CapabilityResolver,
    after_mutation: Callable[[LeaveRequest], None] | None = None,
    commit: bool = True,
) -> LeaveRequestView:
    reason = reason.strip()
    if not reason:
        raise HrDomainError("rejection_reason_required")
    _require_request_in_review_scope(db, request_id, review_scope)
    replay = _replay_transition(
        db,
        actor_user_id=reviewer_user_id,
        request_id=request_id,
        operation_id=client_operation_id,
        event_kind=LeaveAccountEventKind.RELEASE,
        final_status=LeaveRequestStatus.REJECTED,
    )
    if replay is not None:
        if commit: db.commit()
        return replay

    def operation() -> LeaveRequest:
        request, account = _lock_scoped_pending_request_and_account(
            db,
            reviewer_user_id=reviewer_user_id,
            request_id=request_id,
            review_scope=review_scope,
            capability_resolver=capability_resolver,
        )
        account.reserved -= request.workday_count
        account.version += 1
        request.status = LeaveRequestStatus.REJECTED
        request.reviewer_user_id = reviewer_user_id
        request.reviewed_at = datetime.now(timezone.utc)
        request.rejection_reason = reason
        db.add(
            _balance_event(
                account=account,
                request=request,
                actor_user_id=reviewer_user_id,
                operation_id=client_operation_id,
                kind=LeaveAccountEventKind.RELEASE,
                amount=request.workday_count,
            )
        )
        return request

    return _run_write(db, operation, after_mutation=after_mutation, commit=commit)


def cancel_leave_request(
    db: Session,
    actor_user_id: UUID,
    request_id: UUID,
    confirmation_id: UUID,
    client_operation_id: UUID,
    *,
    after_mutation: Callable[[LeaveRequest], None] | None = None,
    commit: bool = True,
) -> LeaveRequestView:
    replay = _replay_transition(
        db,
        actor_user_id=actor_user_id,
        request_id=request_id,
        operation_id=client_operation_id,
        event_kind=LeaveAccountEventKind.RELEASE,
        final_status=LeaveRequestStatus.CANCELLED,
    )
    if replay is not None:
        if commit: db.commit()
        return replay

    def operation() -> LeaveRequest:
        preliminary = find_owned_leave_request(db, actor_user_id, request_id)
        if preliminary is None:
            raise HrDomainError("leave_request_not_found")
        request, account = _lock_pending_request_and_account(
            db, request_id, actor_user_id=actor_user_id
        )
        confirmation = lock_owned_confirmation(
            db, actor_user_id, confirmation_id
        )
        if confirmation is None:
            raise HrDomainError("confirmation_not_found")
        _require_pending_confirmation(confirmation, "hr.cancel_leave_request")
        try:
            confirmed_request_id = UUID(
                str(confirmation.normalized_arguments["request_id"])
            )
        except (KeyError, TypeError, ValueError):
            raise HrDomainError("confirmation_arguments_mismatch") from None
        if confirmed_request_id != request_id:
            raise HrDomainError("confirmation_arguments_mismatch")
        if find_operation_event(db, client_operation_id) is not None:
            raise HrDomainError("operation_id_conflict")

        account.reserved -= request.workday_count
        account.version += 1
        request.status = LeaveRequestStatus.CANCELLED
        request.cancelled_at = datetime.now(timezone.utc)
        request.cancelled_by_user_id = actor_user_id
        db.add(
            _balance_event(
                account=account,
                request=request,
                actor_user_id=actor_user_id,
                operation_id=client_operation_id,
                kind=LeaveAccountEventKind.RELEASE,
                amount=request.workday_count,
            )
        )
        confirmation.status = ToolConfirmationStatus.CONSUMED
        confirmation.client_operation_id = client_operation_id
        confirmation.consumed_at = datetime.now(timezone.utc)
        confirmation.result_resource_type = "leave_request"
        confirmation.result_resource_id = request.id
        return request

    return _run_write(db, operation, after_mutation=after_mutation, commit=commit)


def _run_write(
    db: Session,
    operation: Callable[[], LeaveRequest],
    *,
    after_mutation: Callable[[LeaveRequest], None] | None = None,
    commit: bool = True,
) -> LeaveRequestView:
    try:
        request = operation()
        if after_mutation is not None:
            after_mutation(request)
        if commit:
            db.commit()
            db.refresh(request)
        else:
            db.flush()
        leave_type = db.get(LeaveType, request.leave_type_id)
        if leave_type is None:
            raise HrDomainError("leave_type_not_enabled")
        return _request_view(request, leave_type)
    except HrDomainError:
        if commit: db.rollback()
        raise
    except IntegrityError:
        if commit: db.rollback()
        raise HrDomainError("leave_request_state_conflict") from None
    except Exception:
        if commit: db.rollback()
        raise


def _require_pending_confirmation(
    confirmation: ToolConfirmation,
    tool_name: str,
) -> None:
    if confirmation.tool_name != tool_name:
        raise HrDomainError("confirmation_arguments_mismatch")
    if confirmation.status == ToolConfirmationStatus.EXPIRED:
        raise HrDomainError("confirmation_expired")
    if confirmation.status == ToolConfirmationStatus.CANCELLED:
        raise HrDomainError("confirmation_cancelled")
    if confirmation.status != ToolConfirmationStatus.PENDING:
        raise HrDomainError("confirmation_already_used")
    if confirmation.expires_at <= datetime.now(timezone.utc):
        raise HrDomainError("confirmation_expired")


def _submit_arguments(
    arguments: dict[str, object],
) -> tuple[LeaveTypeCode, date, date, str]:
    try:
        leave_type = LeaveTypeCode(str(arguments["leave_type_code"]))
        start_date = date.fromisoformat(str(arguments["start_date"]))
        end_date = date.fromisoformat(str(arguments["end_date"]))
        reason = str(arguments["reason"]).strip()
    except (KeyError, TypeError, ValueError):
        raise HrDomainError("confirmation_arguments_mismatch") from None
    if not reason:
        raise HrDomainError("confirmation_arguments_mismatch")
    return leave_type, start_date, end_date, reason


def _lock_pending_request_and_account(
    db: Session,
    request_id: UUID,
    *,
    actor_user_id: UUID | None = None,
) -> tuple[LeaveRequest, LeaveAccount]:
    preliminary = db.get(LeaveRequest, request_id)
    if preliminary is None:
        raise HrDomainError("leave_request_not_found")
    employee = lock_employee_by_id(db, preliminary.employee_id)
    if employee is None:
        raise HrDomainError("leave_request_not_found")
    if actor_user_id is not None and employee.user_id != actor_user_id:
        raise HrDomainError("leave_request_not_found")
    account = lock_leave_account(
        db,
        preliminary.employee_id,
        preliminary.leave_type_id,
        preliminary.start_date.year,
    )
    if account is None:
        raise HrDomainError("leave_account_not_found")
    request = (
        lock_owned_request(db, actor_user_id, request_id)
        if actor_user_id is not None
        else lock_request(db, request_id)
    )
    if request is None:
        raise HrDomainError("leave_request_not_found")
    if request.status != LeaveRequestStatus.PENDING:
        raise HrDomainError("leave_request_state_conflict")
    if account.reserved < request.workday_count:
        raise HrDomainError("leave_request_state_conflict")
    return request, account


def _lock_scoped_pending_request_and_account(
    db: Session,
    *,
    reviewer_user_id: UUID,
    request_id: UUID,
    review_scope: CapabilityScope,
    capability_resolver: CapabilityResolver,
) -> tuple[LeaveRequest, LeaveAccount]:
    scoped_row = find_review_request(
        db,
        request_id=request_id,
        scope=review_scope,
        for_update=True,
    )
    reviewer = db.get(User, reviewer_user_id)
    if scoped_row is None or reviewer is None or not reviewer.is_active:
        raise HrDomainError("leave_request_not_found")
    current_scope = capability_resolver.scope_for(
        db,
        reviewer,
        Capability.HR_LEAVE_REVIEW,
    )
    if current_scope is None:
        raise HrDomainError("leave_request_not_found")
    current_row = find_review_request(
        db,
        request_id=request_id,
        scope=current_scope,
    )
    if current_row is None:
        raise HrDomainError("leave_request_not_found")
    request = current_row[0]
    account = lock_leave_account(
        db,
        request.employee_id,
        request.leave_type_id,
        request.start_date.year,
    )
    if account is None:
        raise HrDomainError("leave_account_not_found")
    if request.status != LeaveRequestStatus.PENDING:
        raise HrDomainError("leave_request_state_conflict")
    if account.reserved < request.workday_count:
        raise HrDomainError("leave_request_state_conflict")
    return request, account


def _require_request_in_review_scope(
    db: Session,
    request_id: UUID,
    review_scope: CapabilityScope,
) -> None:
    if find_review_request(
        db,
        request_id=request_id,
        scope=review_scope,
    ) is None:
        raise HrDomainError("leave_request_not_found")


def _balance_event(
    *,
    account: LeaveAccount,
    request: LeaveRequest,
    actor_user_id: UUID,
    operation_id: UUID,
    kind: LeaveAccountEventKind,
    amount: Decimal,
) -> LeaveAccountEvent:
    return LeaveAccountEvent(
        account_id=account.id,
        leave_request_id=request.id,
        actor_user_id=actor_user_id,
        operation_id=operation_id,
        kind=kind,
        amount=amount,
        used_after=account.used,
        reserved_after=account.reserved,
    )


def _replay_transition(
    db: Session,
    *,
    actor_user_id: UUID,
    request_id: UUID,
    operation_id: UUID,
    event_kind: LeaveAccountEventKind,
    final_status: LeaveRequestStatus,
) -> LeaveRequestView | None:
    event = find_operation_event(db, operation_id)
    if event is None:
        return None
    if (
        event.actor_user_id != actor_user_id
        or event.leave_request_id != request_id
        or event.kind != event_kind
    ):
        raise HrDomainError("operation_id_conflict")
    request = db.get(LeaveRequest, request_id)
    if request is None or request.status != final_status:
        raise HrDomainError("operation_id_conflict")
    leave_type = db.get(LeaveType, request.leave_type_id)
    if leave_type is None:
        raise HrDomainError("leave_type_not_enabled")
    return _request_view(request, leave_type)


def _request_view(
    request: LeaveRequest,
    leave_type: LeaveType,
) -> LeaveRequestView:
    return LeaveRequestView(
        id=request.id,
        request_number=request.request_number,
        leave_type_code=leave_type.code,
        leave_type_name=leave_type.display_name,
        start_date=request.start_date,
        end_date=request.end_date,
        workday_count=request.workday_count,
        reason=request.reason,
        status=request.status,
        submitted_at=request.submitted_at,
        reviewed_at=request.reviewed_at,
        rejection_reason=request.rejection_reason,
        cancelled_at=request.cancelled_at,
    )
