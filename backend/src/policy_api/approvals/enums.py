"""Closed enum values used by the frozen approval persistence contract."""

from __future__ import annotations

from policy_api.models import StringEnum


class ApprovalInstanceStatus(StringEnum):
    RUNNING = "running"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ApprovalTaskStatus(StringEnum):
    WAITING = "waiting"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ApprovalDecisionAction(StringEnum):
    APPROVE = "approve"
    REJECT = "reject"


class AssignmentKind(StringEnum):
    USER = "user"
    CAPABILITY = "capability"


class ApprovalCommandKind(StringEnum):
    APPROVE = "approval.approve"
    REJECT = "approval.reject"
    CANCEL = "approval.cancel"


class ApprovalCommandStatus(StringEnum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
