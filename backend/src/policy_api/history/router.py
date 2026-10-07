from __future__ import annotations

import time
from collections.abc import Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DatabaseSession

from policy_api.answers.llm_client import ModelAnswerError
from policy_api.answers.schemas import AnswerOutcome
from policy_api.auth.router import current_identity, database_session, error
from policy_api.history.repository import answer_for_question, citations_for_answer, delete_owned_question, find_owned_question, list_owned_questions, question_id_exists
from policy_api.history.schemas import AnswerResponse, CitationResponse, ClarificationResponse, QuestionCreate, QuestionResponse, QuestionSummary
from policy_api.hr.models import EmployeeProfile
from policy_api.models import Answer, AnswerCitation, AnswerStatus, DocumentChunk, Question, Session, User
from policy_api.rate_limit import limited_response
from policy_api.workbench.events import EventInput
from policy_api.workbench.runtime import WorkbenchRuntime


router = APIRouter(prefix="/questions", tags=["questions"])


def _event_id(event_name: str, resource_id: UUID, discriminator: str = "") -> UUID:
    return uuid5(NAMESPACE_URL, f"policy-assistant:{event_name}:{resource_id}:{discriminator}")


def _organization_snapshot(db: DatabaseSession, user_id: UUID) -> UUID | None:
    return db.scalar(select(EmployeeProfile.organization_unit_id).where(
        EmployeeProfile.user_id == user_id, EmployeeProfile.is_active.is_(True)))


def _message_length_bucket(length: int) -> str:
    if length <= 50: return "0_50"
    if length <= 200: return "51_200"
    if length <= 500: return "201_500"
    return "501_plus"


def _processing_time_bucket(duration_ms: int) -> str:
    if duration_ms < 1_000: return "lt_1s"
    if duration_ms < 3_000: return "1s_3s"
    if duration_ms < 10_000: return "3s_10s"
    return "gte_10s"


def _question_count_bucket(count: int) -> str:
    return {1: "one", 2: "two", 3: "three"}[count]


def _append_knowledge_event(request: Request, db: DatabaseSession, user: User, *,
        event_name: str, resource_id: UUID, outcome: str, duration_ms: int | None,
        dimensions: dict[str, object], discriminator: str = "") -> None:
    runtime = request.app.state.workbench_runtime
    if not isinstance(runtime, WorkbenchRuntime):
        raise RuntimeError("workbench_runtime_not_ready")
    runtime.product_event_emitter.append(db, EventInput(
        event_id=_event_id(event_name, resource_id, discriminator), event_name=event_name,
        module_key="knowledge", actor_user_id=user.id,
        organization_unit_id=_organization_snapshot(db, user.id), role_snapshot=user.role.value,
        request_id=request.state.request_id, outcome=outcome, duration_ms=duration_ms,
        dimensions=dimensions))


def serialize(db: DatabaseSession, question: Question) -> QuestionResponse:
    answer = answer_for_question(db, question.id)
    clarification = None
    if answer is not None and answer.status == AnswerStatus.NEEDS_CLARIFICATION:
        clarification = ClarificationResponse.model_validate(
            answer.clarification_payload
        )
    answer_response = None if answer is None else AnswerResponse(id=answer.id, status=answer.status.value,
        text=answer.text, refusal_reason=answer.refusal_reason.value if answer.refusal_reason else None,
        evidence_score=answer.evidence_score, citations=[CitationResponse(number=c.citation_number,
        chunk_id=c.chunk_id, evidence_snapshot=c.evidence_snapshot,
        source_status="enabled" if document.is_enabled else "disabled",
        document_name=document.display_name, mime_type=document.mime_type,
        page_number=chunk.page_number, heading_path=chunk.heading_path, location=chunk.location)
        for c, chunk, document in citations_for_answer(db, answer.id)],
        clarification=clarification)
    return QuestionResponse(id=question.id, text=question.text, status=question.status,
        created_at=question.created_at, answer=answer_response)


@router.post("", response_model=QuestionResponse)
def ask(payload: QuestionCreate, request: Request, identity: tuple[User, Session] = Depends(current_identity),
        db: DatabaseSession = Depends(database_session)):
    user, _ = identity; question_id = payload.question_id or uuid4()
    retry_after = request.app.state.rate_limiter.check(
        f"question:{user.id}", request.app.state.settings.question_rate_limit_per_minute)
    if retry_after is not None: return limited_response(request, retry_after)
    question = find_owned_question(db, question_id, user.id)
    if question is None and payload.question_id is not None and question_id_exists(db, question_id):
        return error(request, 404, "question_not_found", "The question was not found.")
    if question is not None and question.text != payload.text:
        return error(request, 409, "question_id_conflict", "The question ID belongs to different content.")
    if question is None:
        question = Question(id=question_id, user_id=user.id, text=payload.text, status="processing")
        try:
            db.add(question); db.flush()
            _append_knowledge_event(request, db, user, event_name="question_submitted",
                resource_id=question.id, outcome="submitted", duration_ms=None,
                dimensions={"message_length_bucket": _message_length_bucket(len(question.text))})
            db.commit()
        except Exception:
            db.rollback(); raise
    existing = answer_for_question(db, question.id)
    if existing is not None and existing.status != AnswerStatus.FAILED:
        return serialize(db, question)
    db.commit()
    started = time.perf_counter()
    generate: Callable[[str], AnswerOutcome] = request.app.state.answer_question
    try:
        outcome = generate(question.text)
    except ModelAnswerError as exc:
        duration_ms = int((time.perf_counter()-started)*1000)
        try:
            if existing is not None: db.delete(existing); db.flush()
            question.status = "failed"
            answer = Answer(question_id=question.id, status=AnswerStatus.FAILED, text=None,
                model_name=request.app.state.settings.chat_model, latency_ms=duration_ms)
            db.add(answer); db.flush()
            _append_knowledge_event(request, db, user, event_name="question_abstained",
                resource_id=answer.id, outcome="provider_unavailable", duration_ms=duration_ms,
                dimensions={"error_code": str(exc)})
            db.commit()
        except Exception:
            db.rollback(); raise
        return JSONResponse(status_code=503, content={"code": str(exc), "message": "The model is temporarily unavailable.",
            "request_id": request.state.request_id, "retryable": True, "question_id": str(question.id)})
    duration_ms = int((time.perf_counter()-started)*1000)
    try:
        if existing is not None: db.delete(existing); db.flush()
        question.status = "completed"
        clarification_payload = None
        if outcome.status == "needs_clarification":
            if not outcome.citations:
                raise ValueError("clarification_citations_required")
            clarification_payload = ClarificationResponse(
                questions=list(outcome.clarification_questions)
            ).model_dump(mode="json")
        answer = Answer(question_id=question.id, status=AnswerStatus(outcome.status), text=outcome.text,
            refusal_reason=outcome.refusal_reason, evidence_score=outcome.evidence_score,
            model_name=request.app.state.settings.chat_model, latency_ms=duration_ms,
            clarification_payload=clarification_payload)
        db.add(answer); db.flush()
        for number, chunk_id in enumerate(outcome.citations, start=1):
            chunk = db.get(DocumentChunk, UUID(chunk_id))
            if chunk is not None:
                db.add(AnswerCitation(answer_id=answer.id, chunk_id=chunk.id,
                    citation_number=number, evidence_snapshot=chunk.text))
        if outcome.status == "answered":
            _append_knowledge_event(request, db, user, event_name="question_answered",
                resource_id=answer.id, outcome="answered", duration_ms=duration_ms,
                dimensions={"has_citations": bool(outcome.citations),
                    "processing_time_bucket": _processing_time_bucket(duration_ms)})
        elif outcome.status == "needs_clarification":
            _append_knowledge_event(
                request,
                db,
                user,
                event_name="question_clarification_requested",
                resource_id=answer.id,
                outcome="needs_clarification",
                duration_ms=duration_ms,
                dimensions={
                    "question_count_bucket": _question_count_bucket(
                        len(outcome.clarification_questions)
                    ),
                    "has_citations": True,
                    "processing_time_bucket": _processing_time_bucket(duration_ms),
                },
            )
        else:
            _append_knowledge_event(request, db, user, event_name="question_abstained",
                resource_id=answer.id, outcome="abstained", duration_ms=duration_ms,
                dimensions={"error_code": outcome.refusal_reason.value})
        db.commit()
    except Exception:
        db.rollback(); raise
    return serialize(db, question)


@router.get("", response_model=list[QuestionSummary])
def history(identity: tuple[User, Session] = Depends(current_identity), db: DatabaseSession = Depends(database_session)):
    user, _ = identity
    return [QuestionSummary(id=q.id, text=q.text, status=q.status, created_at=q.created_at) for q in list_owned_questions(db, user.id)]


@router.get("/{question_id}", response_model=QuestionResponse)
def detail(question_id: UUID, request: Request, identity: tuple[User, Session] = Depends(current_identity),
           db: DatabaseSession = Depends(database_session)):
    question = find_owned_question(db, question_id, identity[0].id)
    if question is None: return error(request, 404, "question_not_found", "The question was not found.")
    return serialize(db, question)


@router.delete("/{question_id}", response_model=None)
def delete_question(
    question_id: UUID,
    request: Request,
    identity: tuple[User, Session] = Depends(current_identity),
    db: DatabaseSession = Depends(database_session),
) -> Response:
    if not delete_owned_question(db, question_id, identity[0].id):
        return error(request, 404, "question_not_found", "The question was not found.")
    return Response(status_code=204)
