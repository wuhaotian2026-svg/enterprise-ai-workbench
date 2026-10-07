from __future__ import annotations

import uuid

import httpx
import pytest

from policy_api.tools.errors import ToolError


async def login(client: httpx.AsyncClient, username: str, password: str) -> None:
    response = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 204


@pytest.mark.anyio
async def test_assistant_requires_auth_and_turn_idempotency_has_stable_errors(hr_api_app) -> None:
    app, runtime, users, password, _request_id = hr_api_app
    turn_trace_id = str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        unauthenticated = await client.post("/api/v1/hr/conversations", json={})
        assert unauthenticated.status_code == 401
        assert set(unauthenticated.json()) == {"code", "message", "request_id"}
        await login(client, users["employee"].username, password)
        created = await client.post("/api/v1/hr/conversations", json={"title": "请假咨询"})
        conversation_id = created.json()["id"]
        client_turn_id = str(uuid.uuid4())
        first = await client.post(
            f"/api/v1/hr/conversations/{conversation_id}/turns",
            json={"client_turn_id": client_turn_id, "text": "查询余额"},
            headers={"X-Request-ID": turn_trace_id},
        )
        replay = await client.post(
            f"/api/v1/hr/conversations/{conversation_id}/turns",
            json={"client_turn_id": client_turn_id, "text": "查询余额"},
        )
        conflict = await client.post(
            f"/api/v1/hr/conversations/{conversation_id}/turns",
            json={"client_turn_id": client_turn_id, "text": "不同内容"},
        )
        listing = await client.get("/api/v1/hr/conversations")
        detail = await client.get(f"/api/v1/hr/conversations/{conversation_id}")
    assert created.status_code == first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert conflict.status_code == 409 and conflict.json()["code"] == "client_turn_id_conflict"
    assert set(conflict.json()) == {"code", "message", "request_id"}
    assert listing.json()[0]["title"] == "请假咨询"
    assert detail.json()["turns"][0]["text"] == "查询余额"
    assert detail.json()["turns"][0]["blocks"][0]["type"] == "text"
    assert ("run_turn", turn_trace_id) in runtime.trace_ids


@pytest.mark.anyio
async def test_conversation_archive_is_owner_scoped_and_removed_from_history(
    hr_api_app,
) -> None:
    app, _runtime, users, password, _request_id = hr_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as owner:
        await login(owner, users["employee"].username, password)
        created = await owner.post(
            "/api/v1/hr/conversations", json={"title": "待删除会话"}
        )
        conversation_id = created.json()["id"]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as other:
            await login(other, users["other"].username, password)
            foreign = await other.post(
                f"/api/v1/hr/conversations/{conversation_id}/archive",
                json={},
            )

        archived = await owner.post(
            f"/api/v1/hr/conversations/{conversation_id}/archive",
            json={},
        )
        listing = await owner.get("/api/v1/hr/conversations")
        detail = await owner.get(
            f"/api/v1/hr/conversations/{conversation_id}"
        )
        replay = await owner.post(
            f"/api/v1/hr/conversations/{conversation_id}/archive",
            json={},
        )

    assert foreign.status_code == 404
    assert foreign.json()["code"] == "hr_conversation_not_found"
    assert archived.status_code == 204 and archived.content == b""
    assert all(item["id"] != conversation_id for item in listing.json())
    assert detail.status_code == replay.status_code == 404
    assert detail.json()["code"] == replay.json()["code"] == "hr_conversation_not_found"


@pytest.mark.anyio
async def test_confirmation_confirm_cancel_and_turn_rate_limit(hr_api_app) -> None:
    app, runtime, users, password, _request_id = hr_api_app
    app.state.settings.hr_turn_rate_limit_per_minute = 1
    owner_id = users["employee"].id
    confirm_id = runtime.seed_confirmation(owner_id)
    cancel_id = runtime.seed_confirmation(owner_id)
    confirm_trace_id = str(uuid.uuid4())
    cancel_trace_id = str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, users["employee"].username, password)
        conversation = await client.post("/api/v1/hr/conversations", json={})
        conversation_id = conversation.json()["id"]
        first_turn = await client.post(
            f"/api/v1/hr/conversations/{conversation_id}/turns",
            json={"client_turn_id": str(uuid.uuid4()), "text": "一"},
        )
        limited = await client.post(
            f"/api/v1/hr/conversations/{conversation_id}/turns",
            json={"client_turn_id": str(uuid.uuid4()), "text": "二"},
        )
        confirmed = await client.post(
            f"/api/v1/hr/confirmations/{confirm_id}/confirm",
            json={"client_operation_id": str(uuid.uuid4())},
            headers={"X-Request-ID": confirm_trace_id},
        )
        cancelled = await client.post(
            f"/api/v1/hr/confirmations/{cancel_id}/cancel", json={},
            headers={"X-Request-ID": cancel_trace_id},
        )
    assert first_turn.status_code == 200
    assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
    assert confirmed.json()["type"] == "execution_result"
    assert cancelled.json()["status"] == "cancelled"
    assert ("confirm", confirm_trace_id) in runtime.trace_ids
    assert ("cancel_confirmation", cancel_trace_id) in runtime.trace_ids


@pytest.mark.anyio
async def test_non_retryable_confirmation_error_does_not_expose_internal_metadata(
    hr_api_app,
) -> None:
    app, runtime, users, password, _request_id = hr_api_app
    confirmation_id = runtime.seed_confirmation(users["employee"].id)

    def reject_confirmation(*_args, **_kwargs):
        raise ToolError(
            "tool_execution_non_retryable",
            metadata={
                "failure_stage": "product_event",
                "internal_error_code": "event_dimensions_invalid",
                "schema": "secret-internal-schema",
            },
        )

    runtime.confirm = reject_confirmation
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await login(client, users["employee"].username, password)
        response = await client.post(
            f"/api/v1/hr/confirmations/{confirmation_id}/confirm",
            json={"client_operation_id": str(uuid.uuid4())},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "tool_execution_non_retryable"
    assert set(response.json()) == {"code", "message", "request_id"}
    assert "event_dimensions_invalid" not in response.text
    assert "secret-internal-schema" not in response.text
