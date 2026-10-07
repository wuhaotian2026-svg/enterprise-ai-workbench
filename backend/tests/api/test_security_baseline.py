import importlib.util
from pathlib import Path

import httpx
import pytest

from policy_api.config import Settings
from policy_api.main import create_app


def load_foundation_evaluator():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "evaluate_workbench_foundation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "evaluate_workbench_foundation_security",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("evaluator_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def settings() -> Settings:
    return Settings(database_url="postgresql+psycopg://u:p@localhost/app", session_secret="x" * 32,
        model_base_url="https://example.com/v1", model_api_key="key", chat_model="chat",
        embedding_model="embedding", app_env="production", trusted_hosts="policy.example",
        frontend_origins="https://app.policy.example")


@pytest.mark.asyncio
async def test_production_disables_api_schema_and_rejects_untrusted_host() -> None:
    app = create_app(settings())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://policy.example") as client:
        assert (await client.get("/openapi.json")).status_code == 404
        rejected = await client.get("/api/v1/health/live", headers={"host": "attacker.example"})
        assert rejected.status_code == 400


@pytest.mark.asyncio
async def test_request_id_rejects_overlong_client_value() -> None:
    app = create_app(settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://policy.example",
    ) as client:
        response = await client.get(
            "/api/v1/health/live",
            headers={"X-Request-ID": "x" * 121},
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": "request_id_invalid",
        "message": "X-Request-ID is invalid.",
        "request_id": response.headers["X-Request-ID"],
    }
    assert response.headers["X-Request-ID"] != "x" * 121


@pytest.mark.asyncio
async def test_cookie_authenticated_post_requires_csrf_header() -> None:
    app = create_app(settings(), hr_runtime=object())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://policy.example") as client:
        client.cookies.set("policy_session", "attacker-forced-cookie")
        paths = [
            "/api/v1/auth/logout",
            "/api/v1/hr/conversations",
            f"/api/v1/hr/conversations/{__import__('uuid').uuid4()}/turns",
            f"/api/v1/hr/confirmations/{__import__('uuid').uuid4()}/confirm",
            f"/api/v1/hr/confirmations/{__import__('uuid').uuid4()}/cancel",
            f"/api/v1/hr/leave-requests/{__import__('uuid').uuid4()}/cancel-intent",
            f"/api/v1/hr/review-queue/{__import__('uuid').uuid4()}/approve",
            f"/api/v1/hr/review-queue/{__import__('uuid').uuid4()}/reject",
        ]
        for path in paths:
            response = await client.post(path, json={})
            assert response.status_code == 403
            assert response.json()["code"] == "csrf_failed"


@pytest.mark.asyncio
async def test_cors_allows_only_configured_frontend_origins() -> None:
    app = create_app(settings())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://policy.example") as client:
        allowed = await client.options("/api/v1/health/live", headers={
            "Origin": "https://app.policy.example",
            "Access-Control-Request-Method": "GET",
        })
        rejected = await client.options("/api/v1/health/live", headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "GET",
        })

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://app.policy.example"
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert "access-control-allow-origin" not in rejected.headers


def test_hr_tool_limits_have_bounded_defaults() -> None:
    configured = settings()
    assert configured.tool_max_model_calls == 3
    assert configured.tool_max_read_calls == 4
    assert configured.tool_confirmation_ttl_seconds == 600
    assert configured.hr_turn_rate_limit_per_minute == 20
    assert configured.hr_confirmation_rate_limit_per_minute == 10


def test_foundation_evaluator_cli_rejects_non_test_database_before_output(
    tmp_path: Path,
) -> None:
    evaluator = load_foundation_evaluator()
    output = tmp_path / "must-not-exist.json"

    with pytest.raises(
        evaluator.FoundationEvaluationError,
        match="test_database_required",
    ):
        evaluator.main(
            [
                "--database-url",
                "postgresql+psycopg://u:p@localhost/workbench_production",
                "--output",
                str(output),
            ]
        )

    assert not output.exists()
