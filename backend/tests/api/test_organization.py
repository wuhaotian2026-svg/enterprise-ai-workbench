from __future__ import annotations

import uuid

import httpx
import pytest


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


def operation_id() -> str:
    return str(uuid.uuid4())


def unit_payload(
    suffix: str,
    label: str,
    *,
    parent_id: str | None = None,
    client_operation_id: str | None = None,
) -> dict[str, object]:
    return {
        "client_operation_id": client_operation_id or operation_id(),
        "code": f"WBORG-{suffix.upper()}-{label.upper()}",
        "name": label.title(),
        "parent_id": parent_id,
    }


async def create_unit(
    client: httpx.AsyncClient,
    suffix: str,
    label: str,
    *,
    parent_id: str | None = None,
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/organization/units",
        json=unit_payload(suffix, label, parent_id=parent_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.anyio
async def test_employee_and_hr_are_denied_every_organization_endpoint(
    workbench_api_app,
) -> None:
    app, _runtime, users = workbench_api_app
    missing_id = str(uuid.uuid4())
    for role in ("employee", "hr"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            await login(client, users[role], users["password"])
            requests = [
                client.get("/api/v1/organization/units"),
                client.get("/api/v1/organization/employees"),
                client.get("/api/v1/organization/capability-grants"),
                client.post(
                    "/api/v1/organization/units",
                    json=unit_payload(users["suffix"], f"DENIED-{role}"),
                ),
                client.post(
                    f"/api/v1/organization/units/{missing_id}/update",
                    json={"client_operation_id": operation_id(), "name": "Denied"},
                ),
                client.post(
                    f"/api/v1/organization/employees/{missing_id}/assignment",
                    json={
                        "client_operation_id": operation_id(),
                        "organization_unit_id": None,
                        "manager_employee_id": None,
                    },
                ),
                client.post(
                    "/api/v1/organization/capability-grants",
                    json={
                        "client_operation_id": operation_id(),
                        "user_id": missing_id,
                        "capability": "analytics.view",
                        "scope_kind": "global",
                        "organization_unit_id": None,
                    },
                ),
                client.post(
                    f"/api/v1/organization/capability-grants/{missing_id}/revoke",
                    json={"client_operation_id": operation_id()},
                ),
            ]
            responses = [await request for request in requests]
            assert {response.status_code for response in responses} == {403}
            assert {response.json()["code"] for response in responses} == {
                "capability_required"
            }


@pytest.mark.anyio
async def test_admin_creates_replays_updates_and_deactivates_units(
    workbench_api_app,
) -> None:
    app, _runtime, users = workbench_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["admin"], users["password"])
        create_operation = operation_id()
        payload = unit_payload(
            users["suffix"],
            "ROOT",
            client_operation_id=create_operation,
        )
        created = await client.post("/api/v1/organization/units", json=payload)
        replay = await client.post("/api/v1/organization/units", json=payload)
        assert created.status_code == replay.status_code == 201
        assert replay.json()["id"] == created.json()["id"]

        root = created.json()
        child = await create_unit(
            client,
            users["suffix"],
            "CHILD",
            parent_id=root["id"],
        )
        update_operation = operation_id()
        update_payload = {
            "client_operation_id": update_operation,
            "name": "Child Updated",
        }
        updated = await client.post(
            f"/api/v1/organization/units/{child['id']}/update",
            json=update_payload,
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "Child Updated"
        update_replay = await client.post(
            f"/api/v1/organization/units/{child['id']}/update",
            json=update_payload,
        )
        update_conflict = await client.post(
            f"/api/v1/organization/units/{child['id']}/update",
            json={
                "client_operation_id": update_operation,
                "name": "Different Name",
            },
        )
        assert update_replay.status_code == 200
        assert update_replay.json()["id"] == child["id"]
        assert update_conflict.status_code == 409
        assert update_conflict.json()["code"] == "security_audit_operation_conflict"
        deactivated = await client.post(
            f"/api/v1/organization/units/{child['id']}/update",
            json={"client_operation_id": operation_id(), "is_active": False},
        )
        assert deactivated.status_code == 200
        assert deactivated.json()["is_active"] is False

        listed = await client.get("/api/v1/organization/units")
        assert listed.status_code == 200
        assert {item["id"] for item in listed.json()} >= {root["id"], child["id"]}


@pytest.mark.anyio
async def test_cycles_nonempty_units_and_missing_resources_fail_closed(
    workbench_api_app,
) -> None:
    app, _runtime, users = workbench_api_app
    missing_id = str(uuid.uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["admin"], users["password"])
        root = await create_unit(client, users["suffix"], "TREE-ROOT")
        child = await create_unit(
            client,
            users["suffix"],
            "TREE-CHILD",
            parent_id=root["id"],
        )
        self_parent = await client.post(
            f"/api/v1/organization/units/{child['id']}/update",
            json={"client_operation_id": operation_id(), "parent_id": child["id"]},
        )
        descendant_parent = await client.post(
            f"/api/v1/organization/units/{root['id']}/update",
            json={"client_operation_id": operation_id(), "parent_id": child["id"]},
        )
        assert self_parent.status_code == descendant_parent.status_code == 409
        assert self_parent.json()["code"] == descendant_parent.json()["code"] == (
            "organization_unit_cycle"
        )

        employees = (await client.get("/api/v1/organization/employees")).json()
        employee = next(
            item for item in employees if item["employee_number"] == f"WB-{users['suffix']}"
        )
        assignment = await client.post(
            f"/api/v1/organization/employees/{employee['id']}/assignment",
            json={
                "client_operation_id": operation_id(),
                "organization_unit_id": child["id"],
                "manager_employee_id": None,
            },
        )
        assert assignment.status_code == 200
        not_empty = await client.post(
            f"/api/v1/organization/units/{root['id']}/update",
            json={"client_operation_id": operation_id(), "is_active": False},
        )
        assert not_empty.status_code == 409
        assert not_empty.json()["code"] == "organization_unit_not_empty"

        missing_unit = await client.post(
            f"/api/v1/organization/units/{missing_id}/update",
            json={"client_operation_id": operation_id(), "name": "Missing"},
        )
        missing_employee = await client.post(
            f"/api/v1/organization/employees/{missing_id}/assignment",
            json={
                "client_operation_id": operation_id(),
                "organization_unit_id": None,
                "manager_employee_id": None,
            },
        )
        missing_grant = await client.post(
            f"/api/v1/organization/capability-grants/{missing_id}/revoke",
            json={"client_operation_id": operation_id()},
        )
        assert [
            (response.status_code, response.json()["code"])
            for response in (missing_unit, missing_employee, missing_grant)
        ] == [
            (404, "organization_unit_not_found"),
            (404, "employee_assignment_invalid"),
            (404, "capability_grant_not_found"),
        ]


@pytest.mark.anyio
async def test_grant_revoke_replay_and_reauthorization_reuse_the_same_row(
    workbench_api_app,
) -> None:
    app, _runtime, users = workbench_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["admin"], users["password"])
        employees = (await client.get("/api/v1/organization/employees")).json()
        employee = next(
            item for item in employees if item["employee_number"] == f"WB-{users['suffix']}"
        )
        grant_payload = {
            "client_operation_id": operation_id(),
            "user_id": employee["user_id"],
            "capability": "analytics.view",
            "scope_kind": "global",
            "organization_unit_id": None,
        }
        created = await client.post(
            "/api/v1/organization/capability-grants",
            json=grant_payload,
        )
        assert created.status_code == 201
        grant = created.json()
        create_replay = await client.post(
            "/api/v1/organization/capability-grants",
            json=grant_payload,
        )
        create_conflict = await client.post(
            "/api/v1/organization/capability-grants",
            json={**grant_payload, "capability": "organization.manage"},
        )
        assert create_replay.status_code == 201
        assert create_replay.json()["id"] == grant["id"]
        assert create_conflict.status_code == 409
        assert create_conflict.json()["code"] == "security_audit_operation_conflict"
        revoke_payload = {"client_operation_id": operation_id()}
        revoked = await client.post(
            f"/api/v1/organization/capability-grants/{grant['id']}/revoke",
            json=revoke_payload,
        )
        replay = await client.post(
            f"/api/v1/organization/capability-grants/{grant['id']}/revoke",
            json=revoke_payload,
        )
        assert revoked.status_code == replay.status_code == 200
        assert replay.json()["id"] == grant["id"]
        assert replay.json()["is_active"] is False

        grant_payload["client_operation_id"] = operation_id()
        reactivated = await client.post(
            "/api/v1/organization/capability-grants",
            json=grant_payload,
        )
        assert reactivated.status_code == 201
        assert reactivated.json()["id"] == grant["id"]
        assert reactivated.json()["is_active"] is True
        stale_revoke_replay = await client.post(
            f"/api/v1/organization/capability-grants/{grant['id']}/revoke",
            json=revoke_payload,
        )
        assert stale_revoke_replay.status_code == 409
        assert stale_revoke_replay.json()["code"] == (
            "security_audit_operation_conflict"
        )

        modules = await client.get("/api/v1/workbench/modules")
        assert "hr-review" not in {item["key"] for item in modules.json()["modules"]}


@pytest.mark.anyio
async def test_organization_request_bodies_are_closed(workbench_api_app) -> None:
    app, _runtime, users = workbench_api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await login(client, users["admin"], users["password"])
        payload = unit_payload(users["suffix"], "CLOSED")
        payload["role"] = "admin"
        response = await client.post("/api/v1/organization/units", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_failed"
