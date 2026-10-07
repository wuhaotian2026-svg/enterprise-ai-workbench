from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select

from policy_api.answers.llm_client import ModelAnswerError
from policy_api.answers.schemas import AnswerOutcome
from policy_api.auth.passwords import hash_password
from policy_api.config import Settings
from policy_api.database import create_database_engine, create_session_factory
from policy_api.main import create_app
from policy_api.hr.models import EmployeeProfile
from policy_api.models import Answer, AnswerCitation, AnswerStatus, Document, DocumentChunk, DocumentStatus, Feedback, IngestionRun, IngestionStage, IngestionStatus, Question, RefusalReason, Session, User, UserRole
from policy_api.workbench.capabilities import CapabilityResolver, OrganizationUnit
from policy_api.workbench.catalog import ModuleCatalog
from policy_api.workbench.events import ProductEvent, ProductEventValidationError
from policy_api.workbench.runtime import WorkbenchRuntime


def settings(database_url: str, root: Path) -> Settings:
    credential_placeholder = "-".join(("test", "placeholder"))
    return Settings(app_env="test", database_url=database_url, session_secret="s" * 32,
        frontend_origins="http://localhost:5173", upload_root=root, max_upload_bytes=1024,
        model_base_url="https://models.example.test/v1", model_api_key=credential_placeholder,
        chat_model="chat", embedding_model="embedding")


@pytest.fixture
def questions_app(TEST_DATABASE_URL: str, tmp_path: Path):  # type: ignore[invalid-name]
    engine = create_database_engine(TEST_DATABASE_URL); factory = create_session_factory(engine)
    with factory() as db:
        db.execute(delete(ProductEvent)); db.execute(delete(Feedback)); db.execute(delete(AnswerCitation)); db.execute(delete(Answer)); db.execute(delete(Question)); db.execute(delete(Session)); db.execute(delete(EmployeeProfile)); db.execute(delete(User)); db.execute(delete(OrganizationUnit))
        alice = User(username="alice", password_hash=hash_password("password"), role=UserRole.EMPLOYEE)
        bob = User(username="bob", password_hash=hash_password("password"), role=UserRole.EMPLOYEE)
        unit = OrganizationUnit(code=f"KN-{uuid.uuid4().hex[:8]}", name="Knowledge Unit", is_active=True)
        db.add_all([alice, bob, unit]); db.flush()
        db.add(EmployeeProfile(user_id=alice.id, employee_number=f"KN-{uuid.uuid4().hex[:8]}", display_name="Alice", organization_unit_id=unit.id, hire_date=date(2024, 1, 1), is_active=True)); db.commit()
        user_ids = (alice.id, bob.id)
        unit_id = unit.id
    outcomes: list[AnswerOutcome | Exception] = []
    def answer(question: str) -> AnswerOutcome:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception): raise outcome
        return outcome
    resolver = CapabilityResolver()
    app = create_app(settings(TEST_DATABASE_URL, tmp_path), session_factory=factory, answer_question=answer,
        workbench_runtime=WorkbenchRuntime(module_catalog=ModuleCatalog(resolver), capability_resolver=resolver))
    yield app, factory, outcomes
    with factory() as db:
        question_ids = select(Question.id).where(Question.user_id.in_(user_ids))
        answer_ids = select(Answer.id).where(Answer.question_id.in_(question_ids))
        db.execute(delete(ProductEvent).where(ProductEvent.actor_user_id.in_(user_ids)))
        db.execute(delete(Feedback).where(Feedback.user_id.in_(user_ids)))
        db.execute(delete(AnswerCitation).where(AnswerCitation.answer_id.in_(answer_ids)))
        db.execute(delete(Answer).where(Answer.question_id.in_(question_ids)))
        db.execute(delete(Question).where(Question.user_id.in_(user_ids)))
        db.execute(delete(Session).where(Session.user_id.in_(user_ids)))
        db.execute(delete(EmployeeProfile).where(EmployeeProfile.user_id.in_(user_ids)))
        db.execute(delete(User).where(User.id.in_(user_ids)))
        db.execute(delete(OrganizationUnit).where(OrganizationUnit.id == unit_id))
        db.commit()
    engine.dispose()


async def login(client: httpx.AsyncClient, username: str) -> None:
    assert (await client.post("/api/v1/auth/login", json={"username": username, "password": "password"})).status_code == 204


@pytest.mark.anyio
async def test_question_validation_answer_abstention_and_citation_detail(questions_app) -> None:
    app, factory, outcomes = questions_app
    with factory() as db:
        unique = uuid.uuid4().hex
        document = Document(display_name="travel.txt", storage_key=f"{unique}.txt", sha256=unique * 2,
            mime_type="text/plain", status=DocumentStatus.ENABLED, is_enabled=True); db.add(document); db.flush()
        run = IngestionRun(document_id=document.id, stage=IngestionStage.COMPLETED, status=IngestionStatus.COMPLETED); db.add(run); db.flush()
        chunk = DocumentChunk(ingestion_run_id=run.id, document_id=document.id, sequence=0,
            text="住宿上限500元。", text_hash="b"*64); db.add(chunk); db.commit(); chunk_id = str(chunk.id)
    outcomes.extend([AnswerOutcome("answered", "住宿上限500元。", None, .91, (chunk_id,)),
                     AnswerOutcome("abstained", None, RefusalReason.NO_EVIDENCE, 0, ())])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "alice")
        assert (await client.post("/api/v1/questions", json={"text": "   "})).status_code == 422
        assert (await client.post("/api/v1/questions", json={"text": "x" * 2001})).status_code == 422
        answered = await client.post("/api/v1/questions", json={"text": "住宿上限？"})
        abstained = await client.post("/api/v1/questions", json={"text": "未知制度？"})
        history = await client.get("/api/v1/questions")
        detail = await client.get(f"/api/v1/questions/{answered.json()['id']}")
    assert answered.status_code == 200 and answered.json()["answer"]["text"] == "住宿上限500元。"
    assert abstained.json()["answer"]["status"] == "abstained"
    assert [item["text"] for item in history.json()] == ["未知制度？", "住宿上限？"]
    assert detail.json()["answer"]["evidence_score"] == .91
    assert detail.json()["answer"]["citations"] == [{
        "number": 1, "chunk_id": chunk_id, "evidence_snapshot": "住宿上限500元。",
        "source_status": "enabled", "document_name": "travel.txt", "mime_type": "text/plain",
        "page_number": None, "heading_path": None, "location": None,
    }]
    with factory() as db:
        db.query(Document).filter(Document.id == document.id).update({"status": DocumentStatus.DISABLED, "is_enabled": False}); db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "alice"); disabled_detail = await client.get(f"/api/v1/questions/{answered.json()['id']}")
    assert disabled_detail.json()["answer"]["citations"][0]["source_status"] == "disabled"
    with factory() as db:
        assert db.query(Question).count() == 2 and db.query(Answer).count() == 2
        events = db.scalars(select(ProductEvent).order_by(ProductEvent.created_at)).all()
        assert [event.event_name for event in events] == [
            "question_submitted", "question_answered", "question_submitted", "question_abstained",
        ]
        assert events[0].dimensions == {"message_length_bucket": "0_50"}
        assert events[1].dimensions == {"has_citations": True, "processing_time_bucket": "lt_1s"}
        assert events[2].dimensions == {"message_length_bucket": "0_50"}
        assert events[3].dimensions == {"error_code": "no_evidence"}
        original_unit_id = events[0].organization_unit_id
        assert original_unit_id is not None
        assert all(event.organization_unit_id == original_unit_id for event in events)
        serialized = str([event.dimensions for event in events])
        assert "住宿上限？" not in serialized and "住宿上限500元。" not in serialized

        moved = OrganizationUnit(code=f"KN-{uuid.uuid4().hex[:8]}", name="Moved Unit", is_active=True)
        db.add(moved); db.flush()
        profile = db.scalar(select(EmployeeProfile).where(EmployeeProfile.user_id == events[0].actor_user_id))
        assert profile is not None
        profile.organization_unit_id = moved.id; db.commit()
        historical = db.scalars(select(ProductEvent).order_by(ProductEvent.created_at)).all()
        assert all(event.organization_unit_id == original_unit_id for event in historical)


@pytest.mark.anyio
async def test_clarification_is_persisted_serialized_and_idempotent(questions_app) -> None:
    app, factory, outcomes = questions_app
    with factory() as db:
        unique = uuid.uuid4().hex
        document = Document(
            display_name="travel-clarification.txt",
            storage_key=f"{unique}.txt",
            sha256=unique * 2,
            mime_type="text/plain",
            status=DocumentStatus.ENABLED,
            is_enabled=True,
        )
        db.add(document)
        db.flush()
        run = IngestionRun(
            document_id=document.id,
            stage=IngestionStage.COMPLETED,
            status=IngestionStatus.COMPLETED,
        )
        db.add(run)
        db.flush()
        chunk = DocumentChunk(
            ingestion_run_id=run.id,
            document_id=document.id,
            sequence=0,
            text="其他城市住宿标准为350元每晚。",
            text_hash="c" * 64,
        )
        db.add(chunk)
        db.commit()
        chunk_id = str(chunk.id)

    questions = ("适用哪一城市档位？", "实际住宿几晚？")
    outcomes.append(
        AnswerOutcome(
            "needs_clarification",
            "其他城市住宿标准为350元每晚。",
            None,
            0.87,
            (chunk_id,),
            questions,
        )
    )
    question_id = str(uuid.uuid4())
    payload = {"question_id": question_id, "text": "南京出差三天多少钱？"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await login(client, "alice")
        created = await client.post("/api/v1/questions", json=payload)
        detail = await client.get(f"/api/v1/questions/{question_id}")
        replayed = await client.post("/api/v1/questions", json=payload)

    expected_clarification = {"questions": list(questions)}
    assert created.status_code == 200
    assert created.json() == detail.json() == replayed.json()
    assert created.json()["answer"]["status"] == "needs_clarification"
    assert created.json()["answer"]["text"] == "其他城市住宿标准为350元每晚。"
    assert created.json()["answer"]["refusal_reason"] is None
    assert created.json()["answer"]["clarification"] == expected_clarification

    with factory() as db:
        answer = db.scalar(
            select(Answer).where(Answer.question_id == uuid.UUID(question_id))
        )
        assert answer is not None
        assert answer.status == AnswerStatus.NEEDS_CLARIFICATION
        assert answer.clarification_payload == expected_clarification
        stored_question = db.get(Question, uuid.UUID(question_id))
        assert stored_question is not None
        citations = db.scalars(
            select(AnswerCitation).where(AnswerCitation.answer_id == answer.id)
        ).all()
        assert len(citations) == 1 and str(citations[0].chunk_id) == chunk_id
        event = db.scalar(
            select(ProductEvent).where(
                ProductEvent.event_name == "question_clarification_requested",
                ProductEvent.actor_user_id == stored_question.user_id,
            )
        )
        assert event is not None
        assert event.dimensions == {
            "question_count_bucket": "two",
            "has_citations": True,
            "processing_time_bucket": "lt_1s",
        }
        serialized = str(event.dimensions)
        assert "南京" not in serialized and "适用哪一城市档位" not in serialized


@pytest.mark.anyio
async def test_question_event_validation_rolls_back_each_short_transaction(questions_app, monkeypatch) -> None:
    app, factory, outcomes = questions_app
    runtime = app.state.workbench_runtime
    original_append = runtime.product_event_emitter.append

    def reject_submitted(db, event):
        if event.event_name == "question_submitted":
            raise ProductEventValidationError("event_dimensions_invalid")
        return original_append(db, event)

    monkeypatch.setattr(runtime.product_event_emitter, "append", reject_submitted)
    outcomes.append(AnswerOutcome("answered", "旧实现会错误提交", None, .9, ()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "alice")
        with pytest.raises(ProductEventValidationError, match="event_dimensions_invalid"):
            await client.post("/api/v1/questions", json={"text": "提交应回滚"})
    with factory() as db:
        assert db.query(Question).count() == 0
    assert len(outcomes) == 1
    outcomes.clear()

    def reject_answered(db, event):
        if event.event_name == "question_answered":
            raise ProductEventValidationError("event_dimensions_invalid")
        return original_append(db, event)

    monkeypatch.setattr(runtime.product_event_emitter, "append", reject_answered)
    outcomes.append(AnswerOutcome("answered", "不会提交", None, .9, ()))
    question_id = str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "alice")
        with pytest.raises(ProductEventValidationError, match="event_dimensions_invalid"):
            await client.post("/api/v1/questions", json={"question_id": question_id, "text": "答案应回滚"})
    with factory() as db:
        question = db.get(Question, uuid.UUID(question_id))
        assert question is not None and question.status == "processing"
        assert db.scalar(select(Answer).where(Answer.question_id == question.id)) is None

    def reject_clarification(db, event):
        if event.event_name == "question_clarification_requested":
            raise ProductEventValidationError("event_dimensions_invalid")
        return original_append(db, event)

    monkeypatch.setattr(
        runtime.product_event_emitter, "append", reject_clarification
    )
    with factory() as db:
        unique = uuid.uuid4().hex
        document = Document(
            display_name="rollback-clarification.txt",
            storage_key=f"{unique}.txt",
            sha256=unique * 2,
            mime_type="text/plain",
            status=DocumentStatus.ENABLED,
            is_enabled=True,
        )
        db.add(document)
        db.flush()
        run = IngestionRun(
            document_id=document.id,
            stage=IngestionStage.COMPLETED,
            status=IngestionStatus.COMPLETED,
        )
        db.add(run)
        db.flush()
        chunk = DocumentChunk(
            ingestion_run_id=run.id,
            document_id=document.id,
            sequence=0,
            text="其他城市住宿标准为350元每晚。",
            text_hash="e" * 64,
        )
        db.add(chunk)
        db.commit()
        chunk_id = str(chunk.id)

    outcomes.append(
        AnswerOutcome(
            "needs_clarification",
            "其他城市住宿标准为350元每晚。",
            None,
            0.87,
            (chunk_id,),
            ("实际住宿几晚？",),
        )
    )
    clarification_question_id = str(uuid.uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await login(client, "alice")
        with pytest.raises(
            ProductEventValidationError, match="event_dimensions_invalid"
        ):
            await client.post(
                "/api/v1/questions",
                json={
                    "question_id": clarification_question_id,
                    "text": "澄清事件应回滚",
                },
            )
    with factory() as db:
        question = db.get(Question, uuid.UUID(clarification_question_id))
        assert question is not None and question.status == "processing"
        assert db.scalar(
            select(Answer).where(Answer.question_id == question.id)
        ) is None
        assert db.scalar(
            select(AnswerCitation).join(
                Answer, Answer.id == AnswerCitation.answer_id
            ).where(Answer.question_id == question.id)
        ) is None

    def reject_provider_failure(db, event):
        if event.event_name == "question_abstained":
            raise ProductEventValidationError("event_dimensions_invalid")
        return original_append(db, event)

    monkeypatch.setattr(runtime.product_event_emitter, "append", reject_provider_failure)
    outcomes.append(ModelAnswerError("model_timeout"))
    failed_question_id = str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "bob")
        with pytest.raises(ProductEventValidationError, match="event_dimensions_invalid"):
            await client.post("/api/v1/questions", json={
                "question_id": failed_question_id, "text": "失败答案应回滚",
            })
    with factory() as db:
        failed_question = db.get(Question, uuid.UUID(failed_question_id))
        assert failed_question is not None and failed_question.status == "processing"
        assert db.scalar(select(Answer).where(
            Answer.question_id == failed_question.id)) is None


@pytest.mark.anyio
async def test_history_is_owner_scoped_and_foreign_or_missing_detail_is_safe_404(questions_app) -> None:
    app, _factory, outcomes = questions_app
    outcomes.append(AnswerOutcome("abstained", None, RefusalReason.NO_EVIDENCE, 0, ()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as alice:
        await login(alice, "alice"); created = await alice.post("/api/v1/questions", json={"text": "私有问题"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as bob:
        await login(bob, "bob")
        assert (await bob.get("/api/v1/questions")).json() == []
        foreign = await bob.get(f"/api/v1/questions/{created.json()['id']}")
        missing = await bob.get(f"/api/v1/questions/{uuid.uuid4()}")
        collision = await bob.post("/api/v1/questions", json={"question_id": created.json()["id"], "text": "私有问题"})
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["code"] == missing.json()["code"] == "question_not_found"
    assert collision.status_code == 404 and collision.json()["code"] == "question_not_found"


@pytest.mark.anyio
async def test_retry_is_idempotent_and_model_error_is_retryable(questions_app) -> None:
    app, factory, outcomes = questions_app; question_id = str(uuid.uuid4())
    outcomes.extend([ModelAnswerError("model_timeout"), AnswerOutcome("answered", "已恢复。", None, .8, ())])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "alice")
        failed = await client.post("/api/v1/questions", json={"question_id": question_id, "text": "请重试"})
        retried = await client.post("/api/v1/questions", json={"question_id": question_id, "text": "请重试"})
        replayed = await client.post("/api/v1/questions", json={"question_id": question_id, "text": "请重试"})
    assert failed.status_code == 503 and failed.json()["code"] == "model_timeout" and failed.json()["retryable"] is True
    assert retried.status_code == replayed.status_code == 200 and retried.json() == replayed.json()
    with factory() as db:
        assert db.query(Question).count() == 1 and db.query(Answer).count() == 1
        events = db.scalars(select(ProductEvent).order_by(ProductEvent.created_at)).all()
        assert [event.event_name for event in events] == [
            "question_submitted", "question_abstained", "question_answered",
        ]
        assert events[1].outcome == "provider_unavailable"
        assert events[1].dimensions == {"error_code": "model_timeout"}
        assert events[2].outcome == "answered"


@pytest.mark.anyio
async def test_owner_can_delete_one_question_and_all_dependents(questions_app) -> None:
    app, factory, outcomes = questions_app
    outcomes.extend([
        AnswerOutcome("answered", "住宿上限500元。", None, .91, ()),
        AnswerOutcome("abstained", None, RefusalReason.NO_EVIDENCE, 0, ()),
    ])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.delete(f"/api/v1/questions/{uuid.uuid4()}")).status_code == 401
        await login(client, "alice")
        doomed = await client.post("/api/v1/questions", json={"text": "待删除问题"})
        retained = await client.post("/api/v1/questions", json={"text": "保留问题"})

        doomed_id = uuid.UUID(doomed.json()["id"])
        retained_id = uuid.UUID(retained.json()["id"])
        with factory() as db:
            answer = db.query(Answer).filter(Answer.question_id == doomed_id).one()
            user = db.query(User).filter(User.username == "alice").one()
            unique = uuid.uuid4().hex
            document = Document(
                display_name="delete-test.txt", storage_key=f"{unique}.txt",
                sha256=unique * 2, mime_type="text/plain",
                status=DocumentStatus.ENABLED, is_enabled=True,
            )
            db.add(document); db.flush()
            run = IngestionRun(
                document_id=document.id, stage=IngestionStage.COMPLETED,
                status=IngestionStatus.COMPLETED,
            )
            db.add(run); db.flush()
            chunk = DocumentChunk(
                ingestion_run_id=run.id, document_id=document.id, sequence=0,
                text="住宿上限500元。", text_hash="d" * 64,
            )
            db.add(chunk); db.flush()
            citation = AnswerCitation(
                answer_id=answer.id, chunk_id=chunk.id, citation_number=1,
                evidence_snapshot=chunk.text,
            )
            feedback = Feedback(
                answer_id=answer.id, user_id=user.id, is_helpful=True,
            )
            db.add_all([citation, feedback]); db.commit()
            answer_id = answer.id

        deleted = await client.delete(f"/api/v1/questions/{doomed_id}")
        missing = await client.delete(f"/api/v1/questions/{uuid.uuid4()}")
        history = await client.get("/api/v1/questions")

    assert deleted.status_code == 204 and deleted.content == b""
    assert missing.status_code == 404 and missing.json()["code"] == "question_not_found"
    assert [item["id"] for item in history.json()] == [str(retained_id)]
    with factory() as db:
        assert db.get(Question, doomed_id) is None
        assert db.get(Question, retained_id) is not None
        assert db.query(Answer).filter(Answer.id == answer_id).count() == 0
        assert db.query(AnswerCitation).filter(AnswerCitation.answer_id == answer_id).count() == 0
        assert db.query(Feedback).filter(Feedback.answer_id == answer_id).count() == 0


@pytest.mark.anyio
async def test_foreign_delete_matches_missing_safe_404(questions_app) -> None:
    app, _factory, outcomes = questions_app
    outcomes.append(AnswerOutcome("abstained", None, RefusalReason.NO_EVIDENCE, 0, ()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as alice:
        await login(alice, "alice")
        created = await alice.post("/api/v1/questions", json={"text": "Alice的问题"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as bob:
        await login(bob, "bob")
        foreign = await bob.delete(f"/api/v1/questions/{created.json()['id']}")
        missing = await bob.delete(f"/api/v1/questions/{uuid.uuid4()}")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["code"] == missing.json()["code"] == "question_not_found"
