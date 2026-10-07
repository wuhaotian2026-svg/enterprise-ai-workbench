from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest

from policy_api.hr.calendar import calculate_leave_duration
from policy_api.hr.schemas import HrDomainError, LeaveDuration


@dataclass(frozen=True, slots=True)
class CalendarDay:
    calendar_date: date
    is_workday: bool


def calendar_range(start: date, flags: list[bool]) -> list[CalendarDay]:
    return [
        CalendarDay(calendar_date=start + timedelta(days=offset), is_workday=flag)
        for offset, flag in enumerate(flags)
    ]


def test_duration_is_inclusive_and_respects_holiday_and_adjusted_workday() -> None:
    start = date(2026, 8, 3)
    days = calendar_range(start, [True, True, False, True, True, True, False])

    assert calculate_leave_duration(start, date(2026, 8, 9), days) == LeaveDuration(
        start_date=start,
        end_date=date(2026, 8, 9),
        workday_count=Decimal("5"),
    )


def test_duration_fails_closed_when_any_calendar_date_is_missing() -> None:
    start = date(2026, 8, 3)
    incomplete = calendar_range(start, [True, True, True])

    with pytest.raises(HrDomainError, match="work_calendar_incomplete"):
        calculate_leave_duration(start, date(2026, 8, 6), incomplete)


def test_duration_rejects_reversed_and_zero_workday_ranges() -> None:
    with pytest.raises(HrDomainError, match="leave_date_range_invalid"):
        calculate_leave_duration(date(2026, 8, 4), date(2026, 8, 3), [])

    day = CalendarDay(calendar_date=date(2026, 8, 9), is_workday=False)
    with pytest.raises(HrDomainError, match="leave_duration_zero"):
        calculate_leave_duration(day.calendar_date, day.calendar_date, [day])
