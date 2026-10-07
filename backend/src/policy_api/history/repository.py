from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from policy_api.models import Answer, AnswerCitation, Document, DocumentChunk, Feedback, Question


def find_owned_question(db: Session, question_id: UUID, user_id: UUID) -> Question | None:
    return db.scalar(select(Question).where(Question.id == question_id, Question.user_id == user_id))


def question_id_exists(db: Session, question_id: UUID) -> bool:
    return db.scalar(select(Question.id).where(Question.id == question_id)) is not None


def list_owned_questions(db: Session, user_id: UUID) -> list[Question]:
    return list(db.scalars(select(Question).where(Question.user_id == user_id).order_by(Question.created_at.desc())))


def answer_for_question(db: Session, question_id: UUID) -> Answer | None:
    return db.scalar(select(Answer).where(Answer.question_id == question_id))


def citations_for_answer(db: Session, answer_id: UUID) -> list[tuple[AnswerCitation, DocumentChunk, Document]]:
    statement = (select(AnswerCitation, DocumentChunk, Document)
        .join(DocumentChunk, DocumentChunk.id == AnswerCitation.chunk_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(AnswerCitation.answer_id == answer_id)
        .order_by(AnswerCitation.citation_number))
    return list(db.execute(statement).tuples())


def delete_owned_question(db: Session, question_id: UUID, user_id: UUID) -> bool:
    question = find_owned_question(db, question_id, user_id)
    if question is None:
        return False
    answer_id = db.scalar(select(Answer.id).where(Answer.question_id == question.id))
    if answer_id is not None:
        db.execute(delete(Feedback).where(Feedback.answer_id == answer_id))
        db.execute(delete(AnswerCitation).where(AnswerCitation.answer_id == answer_id))
        db.execute(delete(Answer).where(Answer.id == answer_id))
    db.execute(delete(Question).where(Question.id == question.id))
    db.commit()
    return True
