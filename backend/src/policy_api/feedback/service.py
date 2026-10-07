from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from policy_api.models import Answer, Feedback, Question, utc_now


def upsert_feedback(db: Session, answer_id: UUID, user_id: UUID, is_helpful: bool, reason: str | None) -> Feedback | None:
    owned = db.scalar(select(Answer).join(Question, Answer.question_id == Question.id).where(Answer.id == answer_id, Question.user_id == user_id))
    if owned is None: return None
    feedback = db.scalar(select(Feedback).where(Feedback.answer_id == answer_id, Feedback.user_id == user_id))
    if feedback is None:
        feedback = Feedback(answer_id=answer_id, user_id=user_id, is_helpful=is_helpful, reason=reason); db.add(feedback)
    else:
        feedback.is_helpful = is_helpful; feedback.reason = reason; feedback.updated_at = utc_now()
    db.flush(); db.refresh(feedback); return feedback
