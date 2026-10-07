from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from policy_api.answers.schemas import AnswerOutcome
from policy_api.models import Feedback, RefusalReason
from policy_api.workbench.events import ProductEvent, ProductEventValidationError
from .test_questions import login, questions_app  # noqa: F401


@pytest.mark.anyio
async def test_feedback_upserts_and_preserves_single_record(questions_app) -> None:
    app, factory, outcomes = questions_app
    outcomes.append(AnswerOutcome("answered", "答案", None, .9, ()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "alice")
        created = await client.post("/api/v1/questions", json={"text": "问题"}); answer_id = created.json()["answer"]["id"]
        first = await client.post(f"/api/v1/answers/{answer_id}/feedback", json={"is_helpful": True})
        second = await client.post(f"/api/v1/answers/{answer_id}/feedback", json={"is_helpful": False, "reason": "引用不清楚"})
    assert first.status_code == second.status_code == 200
    assert second.json()["is_helpful"] is False and second.json()["reason"] == "引用不清楚"
    with factory() as db:
        rows = db.scalars(select(Feedback)).all(); assert len(rows) == 1 and rows[0].is_helpful is False
        events = db.scalars(select(ProductEvent).where(ProductEvent.event_name == "answer_feedback_submitted").order_by(ProductEvent.created_at)).all()
        assert [event.dimensions for event in events] == [{"helpful": True}, {"helpful": False}]
        assert all("引用不清楚" not in str(event.dimensions) for event in events)


@pytest.mark.anyio
async def test_feedback_event_failure_rolls_back_feedback(questions_app, monkeypatch) -> None:
    app, factory, outcomes = questions_app
    outcomes.append(AnswerOutcome("answered", "答案", None, .9, ()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "alice")
        created = await client.post("/api/v1/questions", json={"text": "事务问题"})
        answer_id = created.json()["answer"]["id"]

        def reject_feedback(_db, event):
            if event.event_name == "answer_feedback_submitted":
                raise ProductEventValidationError("event_dimensions_invalid")
            raise AssertionError("unexpected event")

        monkeypatch.setattr(app.state.workbench_runtime.product_event_emitter, "append", reject_feedback)
        with pytest.raises(ProductEventValidationError, match="event_dimensions_invalid"):
            await client.post(f"/api/v1/answers/{answer_id}/feedback", json={"is_helpful": False, "reason": "不得提交"})

    with factory() as db:
        assert db.scalars(select(Feedback)).all() == []
        assert db.scalars(select(ProductEvent).where(
            ProductEvent.event_name == "answer_feedback_submitted")).all() == []


@pytest.mark.anyio
async def test_feedback_rejects_invalid_reason_and_foreign_answer(questions_app) -> None:
    app, _factory, outcomes = questions_app
    outcomes.append(AnswerOutcome("abstained", None, RefusalReason.NO_EVIDENCE, 0, ()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as alice:
        await login(alice, "alice"); created = await alice.post("/api/v1/questions", json={"text": "问题"}); answer_id = created.json()["answer"]["id"]
        invalid = await alice.post(f"/api/v1/answers/{answer_id}/feedback", json={"is_helpful": False, "reason": "x" * 501})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as bob:
        await login(bob, "bob"); foreign = await bob.post(f"/api/v1/answers/{answer_id}/feedback", json={"is_helpful": True})
    assert invalid.status_code == 422
    assert foreign.status_code == 404 and foreign.json()["code"] == "answer_not_found"
