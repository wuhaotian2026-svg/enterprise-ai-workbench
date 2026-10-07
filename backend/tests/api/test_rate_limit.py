from __future__ import annotations

import httpx
import pytest

from policy_api.answers.schemas import AnswerOutcome
from policy_api.models import RefusalReason
from .test_questions import login, questions_app  # noqa: F401


@pytest.mark.anyio
async def test_question_limit_is_configurable_and_returns_retry_after(questions_app) -> None:
    app, _factory, outcomes = questions_app
    app.state.settings.question_rate_limit_per_minute = 1
    outcomes.append(AnswerOutcome("abstained", None, RefusalReason.NO_EVIDENCE, 0, ()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "alice")
        first = await client.post("/api/v1/questions", json={"text":"第一次"})
        limited = await client.post("/api/v1/questions", json={"text":"第二次"})
    assert first.status_code == 200
    assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1


@pytest.mark.anyio
async def test_login_limit_is_independent_from_liveness(questions_app) -> None:
    app, _factory, _outcomes = questions_app
    app.state.settings.login_rate_limit_per_minute = 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/v1/auth/login", json={"username":"nobody", "password":"wrong"})
        limited = await client.post("/api/v1/auth/login", json={"username":"nobody", "password":"wrong"})
        live = await client.get("/api/v1/health/live")
    assert first.status_code == 401
    assert limited.status_code == 429 and live.status_code == 200
