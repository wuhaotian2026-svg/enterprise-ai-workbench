from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from policy_api.hr.enums import LeaveRequestStatus, LeaveTypeCode


class HrDomainError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LeaveDuration:
    start_date: date
    end_date: date
    workday_count: Decimal


@dataclass(frozen=True, slots=True)
class LeaveBalanceView:
    account_id: UUID
    leave_type_code: LeaveTypeCode
    leave_type_name: str
    year: int
    entitled: Decimal
    used: Decimal
    reserved: Decimal
    available: Decimal


@dataclass(frozen=True, slots=True)
class LeaveRequestView:
    id: UUID
    request_number: str
    leave_type_code: LeaveTypeCode
    leave_type_name: str
    start_date: date
    end_date: date
    workday_count: Decimal
    reason: str
    status: LeaveRequestStatus
    submitted_at: datetime
    reviewed_at: datetime | None
    rejection_reason: str | None
    cancelled_at: datetime | None


class StrictApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationCreate(StrictApiRequest):
    title: str | None = Field(default=None, max_length=160)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class TurnCreate(StrictApiRequest):
    client_turn_id: UUID
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("turn_text_required")
        return value


class OperationRequest(StrictApiRequest):
    client_operation_id: UUID


class EmptyRequest(StrictApiRequest):
    pass


class RejectRequest(OperationRequest):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def non_blank_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("rejection_reason_required")
        return value
