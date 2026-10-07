from __future__ import annotations

import uuid

import httpx
import pytest

from policy_api.workbench.capabilities import Capability
from policy_api.workbench.catalog import ModuleDefinition


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
async def test_modules_use_session_actor_and_ignore_forged_role_query(
    workbench_api_app,
) -> None:
    app, _runtime, users = workbench_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["employee"], users["password"])
        response = await client.get("/api/v1/workbench/modules?role=admin")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"modules", "catalog_version"}
    assert body["catalog_version"] == "2026-08-23"
    assert [item["key"] for item in body["modules"]] == [
        "knowledge",
        "hr-assistant",
        "my-requests",
        "procurement",
    ]
    assert "organization" not in {item["key"] for item in body["modules"]}


@pytest.mark.anyio
async def test_modules_require_existing_session_identity(workbench_api_app) -> None:
    app, _runtime, _users = workbench_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/workbench/modules")

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert response.json()["request_id"] == response.headers["x-request-id"]


@pytest.mark.anyio
async def test_modules_drop_unknown_server_catalog_keys(workbench_api_app) -> None:
    app, runtime, users = workbench_api_app

    class UnknownCatalog:
        def allowed_modules(self, _db, _user):
            return (
                ModuleDefinition(
                    key="server-injected-component",
                    label="Injected",
                    index="99",
                    capability=Capability.KNOWLEDGE_ASK,
                ),
            )

    runtime.module_catalog = UnknownCatalog()  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["employee"], users["password"])
        response = await client.get("/api/v1/workbench/modules")

    assert response.status_code == 200
    assert response.json()["modules"] == []


@pytest.mark.anyio
async def test_admin_catalog_does_not_inherit_hr_modules(workbench_api_app) -> None:
    app, _runtime, users = workbench_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["admin"], users["password"])
        response = await client.get("/api/v1/workbench/modules")

    assert response.status_code == 200
    assert [item["key"] for item in response.json()["modules"]] == [
        "knowledge",
        "knowledge-admin",
        "organization",
        "analytics",
    ]


@pytest.mark.anyio
async def test_explicit_inbox_grant_exposes_approval_center_module(
    workbench_api_app,
) -> None:
    app, _runtime, users = workbench_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as admin_client:
        await login(admin_client, users["admin"], users["password"])
        employees = await admin_client.get("/api/v1/organization/employees")
        assert employees.status_code == 200
        employee = next(
            item
            for item in employees.json()
            if item["display_name"] == "Workbench Employee"
        )
        grant = await admin_client.post(
            "/api/v1/organization/capability-grants",
            json={
                "client_operation_id": str(uuid.uuid4()),
                "user_id": employee["user_id"],
                "capability": Capability.APPROVAL_INBOX_VIEW.value,
                "scope_kind": "global",
                "organization_unit_id": None,
            },
        )
        assert grant.status_code == 201, grant.text

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as employee_client:
        await login(employee_client, users["employee"], users["password"])
        response = await employee_client.get("/api/v1/workbench/modules")

    assert response.status_code == 200
    assert [item["key"] for item in response.json()["modules"]] == [
        "knowledge",
        "hr-assistant",
        "my-requests",
        "procurement",
        "approval-center",
    ]
