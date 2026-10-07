from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session as DatabaseSession

from policy_api.auth.router import current_identity, database_session
from policy_api.models import Session, User
from policy_api.workbench.capabilities import Capability


class HrPermissionError(RuntimeError):
    pass


def require_hr(
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
) -> tuple[User, Session]:
    user, _session = identity
    workbench_runtime = getattr(request.app.state, "workbench_runtime", None)
    resolver = getattr(workbench_runtime, "capability_resolver", None)
    if resolver is None or not resolver.has(
        db,
        user,
        Capability.HR_LEAVE_REVIEW,
    ):
        raise HrPermissionError("hr_required")
    return identity
