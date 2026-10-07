from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Protocol

from policy_api.hr.schemas import HrDomainError, LeaveDuration


class CalendarDay(Protocol):
    calendar_date: date
    is_workday: bool


def calculate_leave_duration(
    start_date: date,
    end_date: date,
    calendar_days: Iterable[CalendarDay],
) -> LeaveDuration:
    if end_date < start_date:
        raise HrDomainError("leave_date_range_invalid")

    expected_dates = {
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    }
    days_by_date = {day.calendar_date: day for day in calendar_days}
    if set(days_by_date) != expected_dates:
        raise HrDomainError("work_calendar_incomplete")

    workday_count = Decimal(
        sum(1 for day in days_by_date.values() if day.is_workday)
    )
    if workday_count == 0:
        raise HrDomainError("leave_duration_zero")
    return LeaveDuration(
        start_date=start_date,
        end_date=end_date,
        workday_count=workday_count,
    )
