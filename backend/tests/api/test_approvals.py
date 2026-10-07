from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest

from policy_api.tools.errors import ToolError
from policy_api.approvals.router import _call_with_request_id
from tests.api.test_procurement import _login


def test_approval_request_context_does_not_retry_method_body_type_error() -> None:
    calls = 0

    def method(*, request_id=None):
        nonlocal calls
        calls += 1
        raise TypeError("got an unexpected keyword argument 'request_id'")

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        _call_with_request_id(method, (), {}, "server-attempt")

    assert calls == 1


@pytest.mark.anyio
async def test_approval_routes_require_cookie_and_support_closed_filters(procurement_approval_api_app) -> None:
    app, _procurement, runtime, users, password, _request_id, task_id = procurement_approval_api_app
    runtime.seed(status="approved")
    boundary_task = runtime.seed(activated_at=datetime(2030, 1, 2, tzinfo=timezone.utc))
    runtime.seed(activated_at=datetime(2030, 1, 3, tzinfo=timezone.utc))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/approvals/tasks")).status_code == 401
        await _login(client, users["reviewer"].username, password)
        page = await client.get("/api/v1/approvals/tasks", params={"status": "pending", "process_key": "procurement.request", "activated_from": "2030-01-02T08:00:00+08:00", "activated_to": "2030-01-02T08:00:00+08:00", "offset": 0, "limit": 10})
        detail = await client.get(f"/api/v1/approvals/tasks/{task_id}", headers={"X-Request-ID": "approval-trace"})
        invalid = await client.get("/api/v1/approvals/tasks", params={"status": "made_up", "limit": 500})
        invalid_range = await client.get("/api/v1/approvals/tasks", params={"activated_from": "2030-01-03T00:00:00Z", "activated_to": "2030-01-02T00:00:00Z"})
        arbitrary = await client.get("/api/v1/approvals/tasks", params={"assignee": str(users["admin"].id)})
    assert page.status_code == 200 and page.json()["total"] == 1
    assert set(page.json()) == {"items", "offset", "limit", "total"}
    assert page.json()["items"][0]["task_id"] == str(boundary_task)
    assert runtime.list_calls[-1] == {
        "status": "pending", "process_key": "procurement.request",
        "activated_from": datetime(2030, 1, 2, tzinfo=timezone.utc),
        "activated_to": datetime(2030, 1, 2, tzinfo=timezone.utc),
        "offset": 0, "limit": 10,
    }
    assert detail.status_code == 200 and detail.headers["X-Request-ID"] == "approval-trace"
    assert set(detail.json()) == {"task", "subject"}
    assert detail.json()["subject"]["organization"] == {
        "display_name": "API Test Organization"
    }
    assert "organization_unit_id" not in detail.text
    assert invalid.status_code == 422
    assert invalid_range.status_code == 422
    assert arbitrary.status_code == 422


@pytest.mark.anyio
async def test_task_safe_404_and_admin_hr_do_not_inherit_approval_capability(procurement_approval_api_app) -> None:
    app, _procurement, _runtime, users, password, _request_id, task_id = procurement_approval_api_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for key in ("admin", "hr", "other"):
            await _login(client, users[key].username, password)
            listing = await client.get("/api/v1/approvals/tasks")
            hidden = await client.get(f"/api/v1/approvals/tasks/{task_id}")
            forbidden = await client.post(f"/api/v1/approvals/tasks/{task_id}/approve", json={"client_operation_id": str(uuid.uuid4())})
            assert listing.status_code == 200 and listing.json()["items"] == []
            assert hidden.status_code == 404 and hidden.json()["code"] == "approval_task_not_found"
            assert forbidden.status_code == 404 and forbidden.json()["code"] == "approval_task_not_found"
            await client.post("/api/v1/auth/logout")


@pytest.mark.anyio
async def test_approve_and_reject_bodies_are_closed_and_map_conflicts(procurement_approval_api_app) -> None:
    app, _procurement, runtime, users, password, _request_id, task_id = procurement_approval_api_app
    reject_task_id = runtime.seed()
    operation_id = str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await _login(client, users["reviewer"].username, password)
        approved = await client.post(f"/api/v1/approvals/tasks/{task_id}/approve", json={"client_operation_id": operation_id, "comment": "同意"})
        replay = await client.post(f"/api/v1/approvals/tasks/{task_id}/approve", json={"client_operation_id": operation_id, "comment": "同意"})
        collision = await client.post(f"/api/v1/approvals/tasks/{reject_task_id}/approve", json={"client_operation_id": operation_id, "comment": "同意"})
        blank = await client.post(f"/api/v1/approvals/tasks/{reject_task_id}/reject", json={"client_operation_id": str(uuid.uuid4()), "reason": "   "})
        reject_operation = str(uuid.uuid4())
        rejected = await client.post(f"/api/v1/approvals/tasks/{reject_task_id}/reject", json={"client_operation_id": reject_operation, "reason": "不符合要求"})
        reject_replay = await client.post(f"/api/v1/approvals/tasks/{reject_task_id}/reject", json={"client_operation_id": reject_operation, "reason": "不符合要求"})
        changed_reason = await client.post(f"/api/v1/approvals/tasks/{reject_task_id}/reject", json={"client_operation_id": reject_operation, "reason": "不同理由"})
        cross_action = await client.post(f"/api/v1/approvals/tasks/{reject_task_id}/reject", json={"client_operation_id": operation_id, "reason": "不符合要求"})
        forbidden = await client.post(f"/api/v1/approvals/tasks/{reject_task_id}/reject", json={"client_operation_id": str(uuid.uuid4()), "reason": "x", "assignment": "self"})
    assert approved.status_code == 200 and approved.json()["replayed"] is False
    assert set(approved.json()) == {"instance_id", "status", "current_step_key", "replayed"}
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert collision.status_code == 409 and collision.json()["code"] == "approval_operation_id_conflict"
    assert blank.status_code == 422 and blank.json()["code"] == "request_validation_failed"
    assert rejected.status_code == 200
    assert set(rejected.json()) == {"instance_id", "status", "current_step_key", "replayed"}
    assert reject_replay.status_code == 200 and reject_replay.json()["replayed"] is True
    assert changed_reason.status_code == 409 and changed_reason.json()["code"] == "approval_operation_id_conflict"
    assert cross_action.status_code == 409 and cross_action.json()["code"] == "approval_operation_id_conflict"
    assert runtime.mutations[reject_task_id] == 1
    assert forbidden.status_code == 422


@pytest.mark.anyio
async def test_visible_task_with_revoked_action_capability_maps_403(procurement_approval_api_app) -> None:
    app, _procurement, runtime, users, password, _request_id, task_id = procurement_approval_api_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await _login(client, users["reviewer"].username, password)
        detail = await client.get(f"/api/v1/approvals/tasks/{task_id}")
        runtime.revoke_action(task_id, users["reviewer"].id)
        listing_after_revoke = await client.get("/api/v1/approvals/tasks")
        detail_after_revoke = await client.get(f"/api/v1/approvals/tasks/{task_id}")
        forbidden = await client.post(f"/api/v1/approvals/tasks/{task_id}/approve", json={"client_operation_id": str(uuid.uuid4())})
    assert detail.status_code == 200
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "approval_capability_required"
    assert listing_after_revoke.json()["items"] == []
    assert detail_after_revoke.status_code == 404


@pytest.mark.anyio
async def test_approval_response_boundaries_strip_runtime_extras_and_reject_missing_shape(procurement_approval_api_app) -> None:
    app, _procurement, runtime, users, password, _request_id, task_id = procurement_approval_api_app
    original_list = runtime.list_tasks
    def list_with_extras(*args, **kwargs):
        value = original_list(*args, **kwargs)
        value["internal"] = "secret"
        value["items"][0]["assignment"] = "secret"
        return value
    runtime.list_tasks = list_with_extras
    original_detail = runtime.get_task
    def detail_with_extras(*args, **kwargs):
        value = original_detail(*args, **kwargs)
        value["orm_state"] = "secret"
        value["task"]["capability"] = "secret"
        value["subject"]["organization"]["organization_unit_id"] = "secret"
        return value
    runtime.get_task = detail_with_extras
    original_approve = runtime.approve
    runtime.approve = lambda *args, **kwargs: {**original_approve(*args, **kwargs), "actor_user_id": "secret"}
    reject_task_id = runtime.seed()
    original_reject = runtime.reject
    runtime.reject = lambda *args, **kwargs: {**original_reject(*args, **kwargs), "decision_row": "secret"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await _login(client, users["reviewer"].username, password)
        listed = await client.get("/api/v1/approvals/tasks")
        detailed = await client.get(f"/api/v1/approvals/tasks/{task_id}")
        approved = await client.post(f"/api/v1/approvals/tasks/{task_id}/approve", json={"client_operation_id": str(uuid.uuid4())})
        rejected = await client.post(f"/api/v1/approvals/tasks/{reject_task_id}/reject", json={"client_operation_id": str(uuid.uuid4()), "reason": "拒绝"})
    assert "internal" not in listed.json()
    assert "orm_state" not in detailed.json()
    assert "actor_user_id" not in approved.json()
    assert "decision_row" not in rejected.json()
    assert "assignment" not in listed.json()["items"][0]
    assert "capability" not in detailed.json()["task"]
    assert "organization_unit_id" not in detailed.json()["subject"]["organization"]
    runtime.get_task = lambda *args, **kwargs: {"task": {"task_id": task_id}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        await _login(client, users["reviewer"].username, password)
        malformed = await client.get(f"/api/v1/approvals/tasks/{task_id}", headers={"X-Request-ID": "approval-response-invalid"})
    assert malformed.status_code == 500
    assert malformed.headers["X-Request-ID"] == "approval-response-invalid"
    assert malformed.json() == {
        "code": "api_response_invalid",
        "message": "The service produced an invalid response.",
        "request_id": "approval-response-invalid",
    }
    assert "step_key" not in malformed.text and "ValidationError" not in malformed.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "params",
    [
        {"activated_from": "2030-01-02T00:00:00"},
        {"activated_to": "2030-01-02T00:00:00"},
        {
            "activated_from": "2030-01-02T00:00:00+08:00",
            "activated_to": "2030-01-02T00:00:00",
        },
    ],
)
async def test_approval_naive_or_mixed_timezone_filters_are_closed_422(procurement_approval_api_app, params) -> None:
    app, _procurement, _runtime, users, password, _request_id, _task_id = procurement_approval_api_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await _login(client, users["reviewer"].username, password)
        response = await client.get("/api/v1/approvals/tasks", params=params, headers={"X-Request-ID": "approval-timezone-invalid"})
    assert response.status_code == 422
    assert response.headers["X-Request-ID"] == "approval-timezone-invalid"
    assert response.json() == {
        "code": "request_validation_failed",
        "message": "Request validation failed.",
        "request_id": "approval-timezone-invalid",
    }


@pytest.mark.anyio
async def test_approval_scan_limit_error_is_stable_and_correlated(procurement_approval_api_app) -> None:
    app, _procurement, runtime, users, password, _request_id, _task_id = procurement_approval_api_app

    def list_over_limit(*args, **kwargs):
        raise ToolError("approval_page_limit_exceeded")

    runtime.list_tasks = list_over_limit
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await _login(client, users["reviewer"].username, password)
        response = await client.get(
            "/api/v1/approvals/tasks",
            headers={"X-Request-ID": "approval-page-limit"},
        )
    assert response.status_code == 422
    assert response.headers["X-Request-ID"] == "approval-page-limit"
    assert response.json() == {
        "code": "approval_page_limit_exceeded",
        "message": "The approval operation could not be completed.",
        "request_id": "approval-page-limit",
    }
