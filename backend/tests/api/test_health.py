import httpx
import pytest

from policy_api.config import Settings
from policy_api.main import create_app


def build_settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+psycopg://policy:secret@db/policy_test",
        session_secret="s" * 32,
        frontend_origins="http://localhost:5173",
        upload_root="./uploads-test",
        model_base_url="https://models.example.test/v1",
        model_api_key="secret-api-key",
        chat_model="chat-test",
        embedding_model="embedding-test",
    )


@pytest.mark.anyio
async def test_liveness_endpoint_returns_ok() -> None:
    transport = httpx.ASGITransport(app=create_app(build_settings()))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


@pytest.mark.anyio
async def test_unknown_route_uses_stable_json_error_shape() -> None:
    transport = httpx.ASGITransport(app=create_app(build_settings()))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/not-a-route")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert body["message"] == "The requested resource was not found."
    assert body["request_id"] == response.headers["x-request-id"]
