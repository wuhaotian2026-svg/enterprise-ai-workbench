from __future__ import annotations

import httpx
import pytest
import json
import logging

from policy_api.config import Settings
from policy_api.database import create_database_engine, create_session_factory
from policy_api.main import create_app


def settings(database_url: str) -> Settings:
    return Settings(app_env="test", database_url=database_url, session_secret="s"*32,
        frontend_origins="http://localhost:5173", model_base_url="https://models.example.test/v1",
        model_api_key="key", chat_model="chat", embedding_model="embedding")


@pytest.mark.anyio
async def test_live_needs_no_dependencies_and_ready_checks_database_without_model_call(TEST_DATABASE_URL: str) -> None:  # type: ignore[invalid-name]
    model_calls = 0
    def forbidden_model(_question: str):
        nonlocal model_calls; model_calls += 1; raise AssertionError
    bare = create_app(settings(TEST_DATABASE_URL))
    engine = create_database_engine(TEST_DATABASE_URL); ready = create_app(settings(TEST_DATABASE_URL),
        session_factory=create_session_factory(engine), answer_question=forbidden_model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bare), base_url="http://test") as client:
        assert (await client.get("/api/v1/health/live")).json() == {"status":"ok"}
        unavailable = await client.get("/api/v1/health/ready")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ready), base_url="http://test") as client:
        available = await client.get("/api/v1/health/ready")
    assert unavailable.status_code == 503 and unavailable.json()["code"] == "not_ready"
    assert available.status_code == 200 and available.json() == {"status":"ready"}
    assert model_calls == 0
    engine.dispose()


@pytest.mark.anyio
async def test_unmatched_path_value_is_not_written_to_access_log(TEST_DATABASE_URL: str, caplog) -> None:  # type: ignore[invalid-name]
    caplog.set_level(logging.INFO, logger="policy_api")
    app = create_app(settings(TEST_DATABASE_URL))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.get("/api/v1/missing/secret-path-value")
    access = json.loads(caplog.records[-1].message)
    assert access["path"] == "<unmatched>"
    assert "secret-path-value" not in caplog.records[-1].message
