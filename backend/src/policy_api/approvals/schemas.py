"""Closed, immutable application DTOs for approval subject rendering."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import uuid

from pydantic import BaseModel, ConfigDict


class ClosedApprovalDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SubjectSummary(ClosedApprovalDTO):
    request_number: str
    title: str
    total: Decimal
    status: str


class SubjectItem(ClosedApprovalDTO):
    category: str
    name: str
    specification: str | None
    quantity: Decimal
    unit: str
    unit_price: Decimal
    subtotal: Decimal


class SubjectApplicant(ClosedApprovalDTO):
    display_name: str


class SubjectOrganization(ClosedApprovalDTO):
    display_name: str


class SubjectTimelineEntry(ClosedApprovalDTO):
    kind: str
    occurred_at: datetime
    step_key: str | None
    step_label: str | None
    action: str | None
    actor_display_name: str | None
    comment: str | None
    status: str


class SubjectDetail(ClosedApprovalDTO):
    summary: SubjectSummary | None = None
    purpose: str
    needed_by_date: date
    currency: str
    items: tuple[SubjectItem, ...]
    applicant: SubjectApplicant
    organization: SubjectOrganization
    timeline: tuple[SubjectTimelineEntry, ...]


class ApprovalTaskSummary(ClosedApprovalDTO):
    task_id: uuid.UUID
    instance_id: uuid.UUID
    process_key: str
    subject_type: str
    step_key: str
    step_label: str
    status: str
    submitted_at: datetime
    activated_at: datetime | None
    completed_at: datetime | None
    subject: SubjectSummary


class ApprovalTaskDetail(ClosedApprovalDTO):
    task: ApprovalTaskSummary
    subject: SubjectDetail


__all__ = [
    "ApprovalTaskDetail",
    "ApprovalTaskSummary",
    "ClosedApprovalDTO",
    "SubjectApplicant",
    "SubjectDetail",
    "SubjectItem",
    "SubjectOrganization",
    "SubjectSummary",
    "SubjectTimelineEntry",
]
