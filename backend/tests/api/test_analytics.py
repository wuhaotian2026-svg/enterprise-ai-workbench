from __future__ import annotations

from datetime import datetime, timedelta
import os
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from policy_api.models import User
from policy_api.workbench.events import ProductEvent


async def login(
    client: httpx.AsyncClient,
    username: str,
    password: str,
) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 204


@pytest.mark.anyio
async def test_ui_event_is_server_enriched_idempotent_and_closed(
    workbench_api_app,
) -> None:
    app, _runtime, users = workbench_api_app
    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id,
        "event_name": "workbench_module_opened",
        "dimensions": {
            "entry_source": "navigation",
            "module_key": "analytics",
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["admin"], users["password"])
        created = await client.post(
            "/api/v1/analytics/ui-events",
            json=payload,
            headers={"X-Request-ID": "ui/non-uuid/trace"},
        )
        replay = await client.post(
            "/api/v1/analytics/ui-events",
            json=payload,
            headers={"X-Request-ID": "ui/non-uuid/trace"},
        )
        forbidden_name = await client.post(
            "/api/v1/analytics/ui-events",
            json={**payload, "event_id": str(uuid.uuid4()), "event_name": "leave_request_submitted"},
        )
        forbidden_dimension = await client.post(
            "/api/v1/analytics/ui-events",
            json={
                **payload,
                "event_id": str(uuid.uuid4()),
                "dimensions": {**payload["dimensions"], "reason": "sensitive"},
            },
        )

    assert created.status_code == replay.status_code == 204
    assert forbidden_name.status_code == forbidden_dimension.status_code == 422
    assert forbidden_name.json()["code"] == "event_name_not_allowed"
    assert forbidden_dimension.json()["code"] == "event_dimensions_invalid"

    database_url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(database_url)
    try:
        with Session(engine) as db:
            stored = db.scalar(
                select(ProductEvent).where(ProductEvent.event_id == uuid.UUID(event_id))
            )
            assert stored is not None
            actor = db.scalar(select(User).where(User.username == users["admin"]))
            assert actor is not None
            assert stored.actor_user_id == actor.id
            assert stored.organization_unit_id is None
            assert stored.role_snapshot == "admin"
            assert stored.request_id == "ui/non-uuid/trace"
            assert stored.module_key == "analytics"
            assert db.scalar(
                select(func.count()).select_from(ProductEvent).where(
                    ProductEvent.event_id == uuid.UUID(event_id)
                )
            ) == 1
    finally:
        engine.dispose()


ANALYTICS_ENDPOINTS = (
    "/api/v1/analytics/overview",
    "/api/v1/analytics/knowledge",
    "/api/v1/analytics/hr-funnel",
    "/api/v1/analytics/tools",
    "/api/v1/analytics/workflows",
    "/api/v1/analytics/procurement-funnel",
)


@pytest.mark.anyio
async def test_procurement_funnel_uses_same_closed_query_and_scope_errors(
    workbench_api_app,
) -> None:
    app, _runtime, users = workbench_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["employee"], users["password"])
        forbidden = await client.get("/api/v1/analytics/procurement-funnel")

        await login(client, users["admin"], users["password"])
        unknown_scope = await client.get(
            "/api/v1/analytics/procurement-funnel",
            params={"organization_unit_id": str(uuid.uuid4())},
        )
        dynamic_query = await client.get(
            "/api/v1/analytics/procurement-funnel",
            params={"metric": "raw_payload"},
        )

    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "analytics_scope_forbidden"
    assert unknown_scope.status_code == 403
    assert unknown_scope.json()["code"] == "analytics_scope_forbidden"
    assert dynamic_query.status_code == 422
    assert dynamic_query.json()["code"] == "analytics_range_invalid"


@pytest.mark.anyio
async def test_fixed_analytics_endpoints_return_versioned_contract_and_default_window(
    workbench_api_app,
) -> None:
    app, _runtime, users = workbench_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["admin"], users["password"])
        responses = [await client.get(path) for path in ANALYTICS_ENDPOINTS]

    assert all(response.status_code == 200 for response in responses)
    for response in responses:
        payload = response.json()
        assert set(payload) == {
            "from",
            "to",
            "organization_unit_id",
            "metric_version",
            "metrics",
        }
        assert payload["metric_version"] == "v1"
        assert payload["organization_unit_id"] is None
        start = datetime.fromisoformat(payload["from"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(payload["to"].replace("Z", "+00:00"))
        assert end - start == timedelta(days=7)
        assert isinstance(payload["metrics"], dict)


@pytest.mark.anyio
async def test_analytics_requires_capability_and_returns_stable_range_and_scope_errors(
    workbench_api_app,
) -> None:
    app, _runtime, users = workbench_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["employee"], users["password"])
        forbidden = await client.get("/api/v1/analytics/overview")
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "analytics_scope_forbidden"

    invalid_ranges = (
        {"from": "2026-08-17T00:00:00Z", "to": "2026-08-16T00:00:00Z"},
        {"from": "2026-01-01T00:00:00Z", "to": "2026-08-17T00:00:00Z"},
        {"from": "2099-01-01T00:00:00Z", "to": "2099-01-02T00:00:00Z"},
        {"from": "2026-08-10T00:00:00", "to": "2026-08-17T00:00:00"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["admin"], users["password"])
        invalid = [
            await client.get("/api/v1/analytics/overview", params=params)
            for params in invalid_ranges
        ]
        unknown_scope = await client.get(
            "/api/v1/analytics/overview",
            params={"organization_unit_id": str(uuid.uuid4())},
        )
        dynamic_query = await client.get(
            "/api/v1/analytics/overview",
            params={"metric": "arbitrary"},
        )

    assert all(response.status_code == 422 for response in invalid)
    assert all(
        response.json()["code"] == "analytics_range_invalid"
        for response in invalid
    )
    assert unknown_scope.status_code == 403
    assert unknown_scope.json()["code"] == "analytics_scope_forbidden"
    assert dynamic_query.status_code == 422
