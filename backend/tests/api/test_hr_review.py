from __future__ import annotations

import uuid

import httpx
import pytest


async def login(client: httpx.AsyncClient, username: str, password: str) -> None:
    assert (await client.post("/api/v1/auth/login", json={"username": username, "password": password})).status_code == 204


@pytest.mark.anyio
async def test_only_explicit_review_capability_can_enter_review(hr_api_app) -> None:
    app, _runtime, users, password, _request_id = hr_api_app
    for role in ("employee", "hr", "admin"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await login(client, users[role].username, password)
            denied = await client.get("/api/v1/hr/review-queue")
            assert denied.status_code == 403
            assert denied.json()["code"] == "hr_required"
            assert set(denied.json()) == {"code", "message", "request_id"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["reviewer"].username, password)
        assert (await client.get("/api/v1/hr/review-queue")).status_code == 200


@pytest.mark.anyio
async def test_hr_queue_approve_reject_and_closed_review_bodies(hr_api_app) -> None:
    app, runtime, users, password, request_id = hr_api_app
    second_id = runtime.seed_request(users["employee"].id)
    approve_trace_id = str(uuid.uuid4())
    reject_trace_id = str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, users["reviewer"].username, password)
        queue = await client.get("/api/v1/hr/review-queue?status=pending")
        detail = await client.get(f"/api/v1/hr/review-queue/{request_id}")
        approved = await client.post(
            f"/api/v1/hr/review-queue/{request_id}/approve",
            json={"client_operation_id": str(uuid.uuid4())},
            headers={"X-Request-ID": approve_trace_id},
        )
        blank = await client.post(
            f"/api/v1/hr/review-queue/{second_id}/reject",
            json={"client_operation_id": str(uuid.uuid4()), "reason": "   "},
        )
        extra = await client.post(
            f"/api/v1/hr/review-queue/{second_id}/reject",
            json={"client_operation_id": str(uuid.uuid4()), "reason": "不同意", "status": "rejected"},
        )
        rejected = await client.post(
            f"/api/v1/hr/review-queue/{second_id}/reject",
            json={"client_operation_id": str(uuid.uuid4()), "reason": "排班冲突"},
            headers={"X-Request-ID": reject_trace_id},
        )
    assert len(queue.json()) == 2 and detail.json()["id"] == str(request_id)
    assert queue.json()[0]["employee_number"] == "E-API-001"
    assert queue.json()[0]["employee_display_name"] == "API Test Employee"
    assert detail.json()["employee_number"] == "E-API-001"
    assert detail.json()["employee_display_name"] == "API Test Employee"
    assert approved.json()["status"] == "approved"
    assert blank.status_code == extra.status_code == 422
    for response in (blank, extra):
        body = response.json()
        assert set(body) == {"code", "message", "request_id"}
        assert body["code"] == "request_validation_failed"
        assert body["message"] == "Request validation failed."
        uuid.UUID(body["request_id"])
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["rejection_reason"] == "排班冲突"
    assert ("approve", approve_trace_id) in runtime.trace_ids
    assert ("reject", reject_trace_id) in runtime.trace_ids
