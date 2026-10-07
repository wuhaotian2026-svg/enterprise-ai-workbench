from __future__ import annotations

from policy_api.models import StringEnum


class ToolInvocationStatus(StringEnum):
    PROPOSED = "proposed"
    DENIED = "denied"
    VALIDATED = "validated"
    CONFIRMATION_PENDING = "confirmation_pending"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ToolConfirmationStatus(StringEnum):
    PENDING = "pending"
    CONSUMED = "consumed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ToolAuditEventKind(StringEnum):
    PROPOSED = "proposed"
    DENIED = "denied"
    VALIDATED = "validated"
    CONFIRMATION_CREATED = "confirmation_created"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_SUCCEEDED = "execution_succeeded"
    EXECUTION_FAILED = "execution_failed"
