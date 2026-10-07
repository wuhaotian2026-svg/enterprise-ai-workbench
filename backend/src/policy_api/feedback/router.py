from __future__ import annotations

from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as DatabaseSession

from policy_api.auth.router import current_identity, database_session, error
from policy_api.feedback.service import upsert_feedback
from policy_api.hr.models import EmployeeProfile
from policy_api.models import Session, User
from policy_api.workbench.events import EventInput
from policy_api.workbench.runtime import WorkbenchRuntime


router = APIRouter(prefix="/answers", tags=["feedback"])


class FeedbackRequest(BaseModel):
    is_helpful: bool
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        return value or None


class FeedbackResponse(BaseModel):
    id: UUID
    is_helpful: bool
    reason: str | None
    updated_at: datetime


@router.post("/{answer_id}/feedback", response_model=FeedbackResponse)
def submit(answer_id: UUID, payload: FeedbackRequest, request: Request,
           identity: tuple[User, Session] = Depends(current_identity), db: DatabaseSession = Depends(database_session)):
    user = identity[0]
    try:
        feedback = upsert_feedback(db, answer_id, user.id, payload.is_helpful, payload.reason)
        if feedback is None:
            db.rollback()
            return error(request, 404, "answer_not_found", "The answer was not found.")
        runtime = request.app.state.workbench_runtime
        if not isinstance(runtime, WorkbenchRuntime):
            raise RuntimeError("workbench_runtime_not_ready")
        organization_unit_id = db.scalar(select(EmployeeProfile.organization_unit_id).where(
            EmployeeProfile.user_id == user.id, EmployeeProfile.is_active.is_(True)))
        runtime.product_event_emitter.append(db, EventInput(
            event_id=uuid5(NAMESPACE_URL,
                f"policy-assistant:answer_feedback_submitted:{feedback.id}:{request.state.request_id}"),
            event_name="answer_feedback_submitted", module_key="knowledge",
            actor_user_id=user.id, organization_unit_id=organization_unit_id,
            role_snapshot=user.role.value, request_id=request.state.request_id,
            outcome="submitted", duration_ms=None,
            dimensions={"helpful": payload.is_helpful}))
        db.commit(); db.refresh(feedback)
    except Exception:
        db.rollback(); raise
    return FeedbackResponse(id=feedback.id, is_helpful=feedback.is_helpful, reason=feedback.reason, updated_at=feedback.updated_at)
