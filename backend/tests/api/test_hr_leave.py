from __future__ import annotations

import uuid

import httpx
import pytest

from policy_api.hr.schemas import HrDomainError


async def login(client: httpx.AsyncClient, username: str, password: str) -> None:
    assert (await client.post("/api/v1/auth/login", json={"username": username, "password": password})).status_code == 204


@pytest.mark.anyio
async def test_employee_balances_requests_safe_404_and_cancel_intent(hr_api_app) -> None:
    app, runtime, users, password, request_id = hr_api_app
    foreign_id = runtime.seed_request(users["other"].id)
    intent_trace_id = str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, users["employee"].username, password)
        balances = await client.get("/api/v1/hr/leave-balances?year=2033")
        requests = await client.get("/api/v1/hr/leave-requests")
        detail = await client.get(f"/api/v1/hr/leave-requests/{request_id}")
        foreign = await client.get(f"/api/v1/hr/leave-requests/{foreign_id}")
        missing = await client.get(f"/api/v1/hr/leave-requests/{uuid.uuid4()}")
        intent = await client.post(
            f"/api/v1/hr/leave-requests/{request_id}/cancel-intent",
            json={"client_operation_id": str(uuid.uuid4())},
            headers={"X-Request-ID": intent_trace_id},
        )
    assert balances.json()[0]["available"] == "7.00"
    assert [item["id"] for item in requests.json()] == [str(request_id)]
    assert detail.json()["status"] == "pending"
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["code"] == missing.json()["code"] == "leave_request_not_found"
    assert intent.json()["type"] == "confirmation"
    assert runtime.requests[request_id]["status"] == "pending"
    assert ("cancel_intent", intent_trace_id) in runtime.trace_ids


@pytest.mark.anyio
async def test_real_runtime_domain_errors_map_to_stable_http_contract(hr_api_app) -> None:
    app, runtime, users, password, _request_id = hr_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await login(client, users["employee"].username, password)
        for code, expected_status in (
            ("leave_request_not_found", 404),
            ("leave_request_state_conflict", 409),
            ("leave_balance_insufficient", 422),
        ):
            def fail(*_args, error_code=code, **_kwargs):  # type: ignore[no-untyped-def]
                raise HrDomainError(error_code)

            runtime.get_leave_request = fail
            response = await client.get(
                f"/api/v1/hr/leave-requests/{uuid.uuid4()}"
            )
            assert response.status_code == expected_status
            assert response.json()["code"] == code
            assert set(response.json()) == {"code", "message", "request_id"}
