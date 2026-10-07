"""Fail-closed authorization boundary for approval decisions."""

from __future__ import annotations

from typing import Protocol

from policy_api.approvals.enums import ApprovalDecisionAction
from policy_api.approvals.models import ApprovalInstance, ApprovalTask
from policy_api.models import User


class ApprovalAuthorizationPort(Protocol):
    """Authorize a decision against rows already locked by the engine."""

    def authorize(
        self,
        *,
        instance: ApprovalInstance,
        task: ApprovalTask,
        actor: User,
        action: ApprovalDecisionAction,
    ) -> None: ...


__all__ = ["ApprovalAuthorizationPort"]
