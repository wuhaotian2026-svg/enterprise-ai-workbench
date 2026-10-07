from __future__ import annotations

from policy_api.models import StringEnum


class LeaveTypeCode(StringEnum):
    ANNUAL = "annual"
    COMPENSATORY = "compensatory"


class LeaveRequestStatus(StringEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class LeaveAccountEventKind(StringEnum):
    GRANT = "grant"
    RESERVE = "reserve"
    RELEASE = "release"
    CONSUME = "consume"
    ADJUST = "adjust"


class WorkCalendarDayKind(StringEnum):
    WORKDAY = "workday"
    WEEKEND = "weekend"
    HOLIDAY = "holiday"
    ADJUSTED_WORKDAY = "adjusted_workday"
