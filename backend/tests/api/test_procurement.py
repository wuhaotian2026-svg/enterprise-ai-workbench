from __future__ import annotations

import uuid
import inspect
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import delete, select

from policy_api.config import Settings
from policy_api.main import create_app
from policy_api.approvals.models import (
    ApprovalCommandOperation,
    ApprovalDecision,
    ApprovalInstance,
    ApprovalTask,
)
from policy_api.procurement.models import (
    AssistantConversation,
    AssistantTurn,
    ProcurementCommandOperation,
    ProcurementRequest,
    ProcurementRequestItem,
)
from policy_api.procurement.observability import attempt_audit_operation_id
from policy_api.procurement.router import (
    _call_with_request_id,
    preview_request as preview_endpoint,
)
from policy_api.tools.models import ToolAuditEvent, ToolConfirmation, ToolInvocation
from policy_api.workbench.audit import SecurityAuditEvent, append_security_audit
from policy_api.workbench.events import ProductEvent


SUBMIT = {
    "client_operation_id": None,
    "title": "办公耗材",
    "purpose": "团队使用",
    "needed_by_date": "2035-01-01",
    "currency": "CNY",
    "items": [{
        "category_code": "office_supplies", "item_name": "签字笔",
        "specification": None, "quantity": "2", "unit": "盒",
        "estimated_unit_price": "10.00",
    }],
}

PREVIEW = {key: value for key, value in SUBMIT.items() if key != "client_operation_id"}

BUSINESS_RESOURCE_MODELS = (
    ProcurementRequest,
    ProcurementRequestItem,
    ProcurementCommandOperation,
    ApprovalInstance,
    ApprovalTask,
    ApprovalDecision,
    ApprovalCommandOperation,
    AssistantConversation,
    AssistantTurn,
    ToolInvocation,
    ToolConfirmation,
    ToolAuditEvent,
    SecurityAuditEvent,
    ProductEvent,
)


def _business_resource_counts(app) -> tuple[int, ...]:
    with app.state.session_factory() as db:
        return tuple(db.query(model).count() for model in BUSINESS_RESOURCE_MODELS)


async def _login(client: httpx.AsyncClient, username: str, password: str) -> None:
    response = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 204


def test_procurement_request_context_does_not_retry_method_body_type_error() -> None:
    calls = 0

    def method(*, request_trace_id=None):
        nonlocal calls
        calls += 1
        raise TypeError("got an unexpected keyword argument 'request_trace_id'")

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        _call_with_request_id(method, (), {}, "server-attempt")

    assert calls == 1


@pytest.mark.anyio
async def test_preview_is_authenticated_closed_and_creates_no_business_resources(
    procurement_approval_api_app,
) -> None:
    app, runtime, _approval, users, password, _request_id, _task_id = (
        procurement_approval_api_app
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthenticated = await client.post(
            "/api/v1/procurement/requests/preview", json=PREVIEW
        )
        await _login(client, users["employee"].username, password)
        before = _business_resource_counts(app)
        previewed = await client.post(
            "/api/v1/procurement/requests/preview",
            json={
                **PREVIEW,
                "items": [
                    PREVIEW["items"][0],
                    {
                        **PREVIEW["items"][0],
                        "item_name": "测试耗材",
                        "quantity": "0.33",
                        "estimated_unit_price": "0.05",
                    },
                ],
            },
        )
        after = _business_resource_counts(app)
        forbidden_operation = await client.post(
            "/api/v1/procurement/requests/preview",
            json={**PREVIEW, "client_operation_id": str(uuid.uuid4())},
        )
        forbidden_total = await client.post(
            "/api/v1/procurement/requests/preview",
            json={**PREVIEW, "total": "20.00"},
        )

    assert unauthenticated.status_code == 401
    assert previewed.status_code == 200
    assert previewed.json() == {
        "currency": "CNY",
        "subtotals": ["20.00", "0.02"],
        "total": "20.02",
    }
    assert before == after
    assert len(runtime.preview_calls) == 1
    assert forbidden_operation.status_code == 422
    assert forbidden_operation.json()["code"] == "request_validation_failed"
    assert forbidden_total.status_code == 422
    assert forbidden_total.json()["code"] == "request_validation_failed"
    assert "db" not in inspect.signature(preview_endpoint).parameters
    assert "actor" not in inspect.signature(preview_endpoint).parameters


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("request_payload", "runtime_result"),
    [
        (
            PREVIEW,
            {
                "currency": "CNY",
                "subtotals": ["10.00", "10.00"],
                "total": "20.00",
            },
        ),
        (
            PREVIEW,
            {
                "currency": "CNY",
                "subtotals": ["20.00"],
                "total": "20.00",
                "internal_calculation_trace": "must-not-project",
            },
        ),
        (
            PREVIEW,
            {"currency": "CNY", "subtotals": ["19.99"], "total": "20.00"},
        ),
        (
            PREVIEW,
            {"currency": "CNY", "subtotals": ["19.99"], "total": "19.99"},
        ),
        (
            PREVIEW,
            {
                "currency": "CNY",
                "subtotals": ["1000000000000.00"],
                "total": "20.00",
            },
        ),
        (
            {
                **PREVIEW,
                "items": [
                    PREVIEW["items"][0],
                    {
                        **PREVIEW["items"][0],
                        "item_name": "显示器支架",
                        "quantity": "3",
                        "estimated_unit_price": "2.00",
                    },
                ],
            },
            {
                "currency": "CNY",
                "subtotals": ["6.00", "20.00"],
                "total": "26.00",
            },
        ),
    ],
)
async def test_preview_malformed_runtime_response_fails_closed(
    procurement_approval_api_app,
    request_payload: dict[str, object],
    runtime_result: dict[str, object],
) -> None:
    app, runtime, _approval, users, password, _request_id, _task_id = (
        procurement_approval_api_app
    )
    runtime.preview_request = lambda _request_input: runtime_result
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        await _login(client, users["employee"].username, password)
        malformed = await client.post(
            "/api/v1/procurement/requests/preview",
            json=request_payload,
            headers={"X-Request-ID": "procurement-preview-invalid"},
        )

    assert malformed.status_code == 500
    assert malformed.json() == {
        "code": "api_response_invalid",
        "message": "The service produced an invalid response.",
        "request_id": "procurement-preview-invalid",
    }
    assert "ValidationError" not in malformed.text


@pytest.mark.anyio
async def test_reused_correlation_id_gets_distinct_server_attempt_audits(
    procurement_approval_api_app,
) -> None:
    app, runtime, _approval, users, password, _request_id, _task_id = (
        procurement_approval_api_app
    )
    confirmation_id = uuid.uuid4()
    operation_id = uuid.uuid4()

    def confirm_submission(
        db, *, actor, confirmation_id, client_operation_id, request_id
    ):
        append_security_audit(
            db,
            event_name="procurement_operation_replayed",
            actor_user_id=actor.id,
            target_type="procurement_request",
            target_id=None,
            operation_id=attempt_audit_operation_id(
                actor.id,
                client_operation_id,
                request_id,
                "procurement.submit_request",
                "replayed",
            ),
            outcome="replayed",
            request_id=request_id,
            summary={
                "procurement_request_id": None,
                "organization_unit_id": None,
                "stage": "submission",
                "code": "exact_replay",
            },
        )
        db.commit()
        return {
            "type": "execution_result",
            "resource_type": "procurement_request",
            "resource_id": uuid.uuid4(),
            "replayed": True,
        }

    runtime.confirm_submission = confirm_submission
    payload = {"client_operation_id": str(operation_id)}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _login(client, users["employee"].username, password)
        responses = [
            await client.post(
                f"/api/v1/procurement/confirmations/{confirmation_id}/confirm",
                json=payload,
                headers={"X-Request-ID": "shared-correlation"},
            )
            for _ in range(2)
        ]

    with app.state.session_factory() as db:
        audits = tuple(
            db.scalars(
                select(SecurityAuditEvent)
                .where(
                    SecurityAuditEvent.actor_user_id == users["employee"].id,
                    SecurityAuditEvent.event_name
                    == "procurement_operation_replayed",
                )
                .order_by(SecurityAuditEvent.created_at)
            )
        )
        db.execute(
            delete(SecurityAuditEvent).where(
                SecurityAuditEvent.actor_user_id == users["employee"].id
            )
        )
        db.commit()

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.headers["X-Request-ID"] for response in responses] == [
        "shared-correlation",
        "shared-correlation",
    ]
    assert len(audits) == 2
    assert len({audit.operation_id for audit in audits}) == 2
    assert len({audit.request_id for audit in audits}) == 2
    assert all(uuid.UUID(audit.request_id or "") for audit in audits)
    assert all(audit.request_id != "shared-correlation" for audit in audits)


@pytest.mark.anyio
async def test_procurement_routes_require_cookie_and_propagate_request_id(procurement_approval_api_app) -> None:
    app, _runtime, _approval, users, password, request_id, _task_id = procurement_approval_api_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/procurement/requests")).status_code == 401
        await _login(client, users["employee"].username, password)
        response = await client.get(f"/api/v1/procurement/requests/{request_id}", headers={"X-Request-ID": "procurement-trace-1"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "procurement-trace-1"
    assert "owner_user_id" not in response.json()
    assert set(response.json()) == {"id", "summary", "purpose", "needed_by_date", "currency", "items", "applicant", "organization", "timeline"}
    assert response.json()["organization"] == {
        "display_name": "API Test Organization"
    }
    assert "organization_unit_id" not in response.text


@pytest.mark.anyio
async def test_submit_is_closed_and_preserves_exact_replay_conflict(procurement_approval_api_app) -> None:
    app, runtime, _approval, users, password, _request_id, _task_id = procurement_approval_api_app
    operation_id = str(uuid.uuid4())
    payload = {**SUBMIT, "client_operation_id": operation_id}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await _login(client, users["employee"].username, password)
        first = await client.post("/api/v1/procurement/requests", json=payload, headers={"X-Request-ID": "submit-trace"})
        replay = await client.post("/api/v1/procurement/requests", json=payload)
        conflict = await client.post("/api/v1/procurement/requests", json={**payload, "title": "不同内容"})
        forbidden = await client.post("/api/v1/procurement/requests", json={**payload, "actor": str(users["employee"].id)})
    assert first.status_code == 201 and first.json()["replayed"] is False
    assert set(first.json()) == {"id", "request_number", "title", "total", "status", "submitted_at", "replayed"}
    assert "owner_user_id" not in first.json()
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert conflict.status_code == 409 and conflict.json()["code"] == "operation_id_conflict"
    assert forbidden.status_code == 422 and forbidden.json()["code"] == "request_validation_failed"
    assert runtime.trace_ids[0] != "submit-trace"
    assert uuid.UUID(runtime.trace_ids[0] or "")


@pytest.mark.anyio
async def test_procurement_list_paginates_filters_and_owner_detail_is_safe_404(procurement_approval_api_app) -> None:
    app, runtime, _approval, users, password, request_id, _task_id = procurement_approval_api_app
    included_id = runtime.seed(
        users["employee"].id,
        status="cancelled",
        submitted_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
    )
    runtime.seed(
        users["employee"].id,
        status="cancelled",
        submitted_at=datetime(2030, 1, 3, tzinfo=timezone.utc),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await _login(client, users["employee"].username, password)
        page = await client.get("/api/v1/procurement/requests", params={"status": "cancelled", "submitted_from": "2030-01-02", "submitted_to": "2030-01-02", "offset": 0, "limit": 10})
        invalid = await client.get("/api/v1/procurement/requests", params={"status": "anything", "limit": 1000})
        invalid_range = await client.get("/api/v1/procurement/requests", params={"submitted_from": "2030-01-03", "submitted_to": "2030-01-02"})
        arbitrary = await client.get("/api/v1/procurement/requests", params={"filter": "status = 'approved'"})
        await client.post("/api/v1/auth/logout")
        await _login(client, users["other"].username, password)
        hidden = await client.get(f"/api/v1/procurement/requests/{request_id}")
    assert page.status_code == 200
    assert set(page.json()) == {"items", "offset", "limit", "total"}
    assert set(page.json()["items"][0]) == {"id", "request_number", "title", "total", "status", "submitted_at"}
    assert page.json()["total"] == 1 and page.json()["items"][0]["id"] == str(included_id)
    assert invalid.status_code == 422
    assert invalid_range.status_code == 422
    assert arbitrary.status_code == 422
    assert hidden.status_code == 404 and hidden.json()["code"] == "procurement_request_not_found"


@pytest.mark.anyio
async def test_withdraw_body_is_closed_and_maps_state_conflict(procurement_approval_api_app) -> None:
    app, runtime, _approval, users, password, request_id, _task_id = procurement_approval_api_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await _login(client, users["employee"].username, password)
        operation_id = str(uuid.uuid4())
        ok = await client.post(f"/api/v1/procurement/requests/{request_id}/withdraw", json={"client_operation_id": operation_id})
        replay = await client.post(f"/api/v1/procurement/requests/{request_id}/withdraw", json={"client_operation_id": operation_id})
        other_id = runtime.seed(users["employee"].id)
        conflict = await client.post(f"/api/v1/procurement/requests/{other_id}/withdraw", json={"client_operation_id": operation_id})
        forbidden = await client.post(f"/api/v1/procurement/requests/{request_id}/withdraw", json={"client_operation_id": str(uuid.uuid4()), "status": "cancelled"})
    assert ok.status_code == 200
    assert set(ok.json()) == {"instance_id", "status", "current_step_key", "replayed"}
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert runtime.withdraw_mutations[request_id] == 1
    assert conflict.status_code == 409 and conflict.json()["code"] == "approval_operation_id_conflict"
    assert forbidden.status_code == 422


@pytest.mark.anyio
async def test_procurement_response_boundaries_strip_runtime_extras_and_reject_missing_shape(procurement_approval_api_app) -> None:
    app, runtime, _approval, users, password, request_id, _task_id = procurement_approval_api_app
    original_list = runtime.list_requests
    def list_with_extras(*args, **kwargs):
        value = original_list(*args, **kwargs)
        value["internal"] = "secret"
        value["items"][0]["_sa_instance_state"] = "secret"
        return value
    runtime.list_requests = list_with_extras
    original_detail = runtime.get_request
    def detail_with_extras(*args, **kwargs):
        value = original_detail(*args, **kwargs)
        value["orm_state"] = "secret"
        value["summary"]["organization_unit_id"] = "secret"
        value["organization"]["organization_unit_id"] = "secret"
        return value
    runtime.get_request = detail_with_extras
    original_withdraw = runtime.withdraw_request
    runtime.withdraw_request = lambda *args, **kwargs: {**original_withdraw(*args, **kwargs), "actor_user_id": "secret"}
    original_submit = runtime.submit_request
    runtime.submit_request = lambda *args, **kwargs: {**original_submit(*args, **kwargs), "manager_user_id": "secret"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await _login(client, users["employee"].username, password)
        listed = await client.get("/api/v1/procurement/requests")
        detailed = await client.get(f"/api/v1/procurement/requests/{request_id}")
        withdrawn = await client.post(f"/api/v1/procurement/requests/{request_id}/withdraw", json={"client_operation_id": str(uuid.uuid4())})
        submitted = await client.post("/api/v1/procurement/requests", json={**SUBMIT, "client_operation_id": str(uuid.uuid4())})
    assert "internal" not in listed.json()
    assert "orm_state" not in detailed.json()
    assert "actor_user_id" not in withdrawn.json()
    assert "manager_user_id" not in submitted.json()
    assert "_sa_instance_state" not in listed.json()["items"][0]
    assert "organization_unit_id" not in detailed.json()["summary"]
    assert "organization_unit_id" not in detailed.json()["organization"]
    runtime.get_request = lambda *args, **kwargs: {"id": request_id}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        await _login(client, users["employee"].username, password)
        malformed = await client.get(f"/api/v1/procurement/requests/{request_id}", headers={"X-Request-ID": "procurement-response-invalid"})
    assert malformed.status_code == 500
    assert malformed.headers["X-Request-ID"] == "procurement-response-invalid"
    assert malformed.json() == {
        "code": "api_response_invalid",
        "message": "The service produced an invalid response.",
        "request_id": "procurement-response-invalid",
    }
    assert "summary" not in malformed.text and "ValidationError" not in malformed.text


@pytest.mark.anyio
async def test_production_csrf_and_existing_rate_limit_apply_to_procurement(procurement_approval_api_app) -> None:
    base_app, runtime, approval, users, password, _request_id, _task_id = procurement_approval_api_app
    settings = Settings(
        app_env="production",
        database_url=base_app.state.settings.database_url,
        session_secret="s" * 32,
        model_base_url="https://models.example.test/v1",
        model_api_key="key",
        chat_model="chat",
        embedding_model="embedding",
        hr_confirmation_rate_limit_per_minute=1,
    )
    app = create_app(
        settings,
        session_factory=base_app.state.session_factory,
        procurement_runtime=runtime,
        approval_runtime=approval,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        await _login(client, users["employee"].username, password)
        csrf = client.cookies.get("policy_csrf")
        preview_missing = await client.post(
            "/api/v1/procurement/requests/preview", json=PREVIEW
        )
        previewed = await client.post(
            "/api/v1/procurement/requests/preview",
            json=PREVIEW,
            headers={"X-CSRF-Token": csrf},
        )
        missing = await client.post("/api/v1/procurement/requests", json={**SUBMIT, "client_operation_id": str(uuid.uuid4())})
        headers = {"X-CSRF-Token": csrf}
        first = await client.post("/api/v1/procurement/requests", json={**SUBMIT, "client_operation_id": str(uuid.uuid4())}, headers=headers)
        limited = await client.post("/api/v1/procurement/requests", json={**SUBMIT, "client_operation_id": str(uuid.uuid4())}, headers=headers)
    assert preview_missing.status_code == 403
    assert preview_missing.json()["code"] == "csrf_failed"
    assert previewed.status_code == 200
    assert missing.status_code == 403 and missing.json()["code"] == "csrf_failed"
    assert first.status_code == 201
    assert limited.status_code == 429 and "Retry-After" in limited.headers


@pytest.mark.anyio
async def test_procurement_confirmation_route_is_owner_bound_closed_and_propagates_request_id(
    procurement_approval_api_app,
) -> None:
    app, runtime, _approval, users, password, _request_id, _task_id = procurement_approval_api_app
    confirmation_id = uuid.uuid4()
    calls: list[dict[str, object]] = []

    def confirm_submission(_db, *, actor, confirmation_id, client_operation_id, request_id):
        calls.append(
            {
                "actor": actor.id,
                "confirmation_id": confirmation_id,
                "client_operation_id": client_operation_id,
                "request_id": request_id,
            }
        )
        return {
            "type": "execution_result",
            "resource_type": "procurement_request",
            "resource_id": uuid.uuid4(),
            "replayed": False,
        }

    runtime.confirm_submission = confirm_submission
    payload = {"client_operation_id": str(uuid.uuid4())}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthenticated = await client.post(
            f"/api/v1/procurement/confirmations/{confirmation_id}/confirm",
            json=payload,
        )
        await _login(client, users["employee"].username, password)
        forbidden = await client.post(
            f"/api/v1/procurement/confirmations/{confirmation_id}/confirm",
            json={**payload, "normalized_arguments": {"title": "injected"}},
        )
        confirmed = await client.post(
            f"/api/v1/procurement/confirmations/{confirmation_id}/confirm",
            json=payload,
            headers={"X-Request-ID": "procurement-confirm-trace"},
        )

    assert unauthenticated.status_code == 401
    assert forbidden.status_code == 422
    assert confirmed.status_code == 200
    assert confirmed.headers["X-Request-ID"] == "procurement-confirm-trace"
    assert set(confirmed.json()) == {
        "type", "resource_type", "resource_id", "replayed"
    }
    assert len(calls) == 1
    assert calls[0] == {
        "actor": users["employee"].id,
        "confirmation_id": confirmation_id,
        "client_operation_id": uuid.UUID(payload["client_operation_id"]),
        "request_id": calls[0]["request_id"],
    }
    assert calls[0]["request_id"] != "procurement-confirm-trace"
    assert uuid.UUID(str(calls[0]["request_id"]))


@pytest.mark.anyio
async def test_procurement_confirmation_cancel_route_is_closed_and_owner_bound(
    procurement_approval_api_app,
) -> None:
    app, runtime, _approval, users, password, _request_id, _task_id = procurement_approval_api_app
    confirmation_id = uuid.uuid4()
    calls: list[tuple[uuid.UUID, uuid.UUID, str | None]] = []

    def cancel_confirmation(_db, actor_id, confirmation_id, *, request_id=None):
        calls.append((actor_id, confirmation_id, request_id))
        return {"confirmation_id": confirmation_id, "status": "cancelled"}

    runtime.cancel_confirmation = cancel_confirmation
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _login(client, users["employee"].username, password)
        forbidden = await client.post(
            f"/api/v1/procurement/confirmations/{confirmation_id}/cancel",
            json={"status": "cancelled"},
        )
        cancelled = await client.post(
            f"/api/v1/procurement/confirmations/{confirmation_id}/cancel",
            json={},
            headers={"X-Request-ID": "procurement-cancel-trace"},
        )

    assert forbidden.status_code == 422
    assert cancelled.status_code == 200
    assert cancelled.json() == {
        "confirmation_id": str(confirmation_id), "status": "cancelled"
    }
    assert len(calls) == 1
    assert calls[0][:2] == (users["employee"].id, confirmation_id)
    assert calls[0][2] != "procurement-cancel-trace"
    assert uuid.UUID(calls[0][2] or "")


@pytest.mark.anyio
async def test_procurement_conversation_bodies_reject_overlong_and_blank_text(
    procurement_approval_api_app,
) -> None:
    app, runtime, _approval, users, password, _request_id, _task_id = procurement_approval_api_app
    runtime.create_conversation = lambda *_args, **_kwargs: {}
    runtime.run_turn = lambda *_args, **_kwargs: {}
    conversation_id = uuid.uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _login(client, users["employee"].username, password)
        overlong = await client.post(
            "/api/v1/procurement/conversations", json={"title": "x" * 161}
        )
        blank = await client.post(
            f"/api/v1/procurement/conversations/{conversation_id}/turns",
            json={"client_turn_id": str(uuid.uuid4()), "text": "   "},
        )
    assert overlong.status_code == 422
    assert blank.status_code == 422


@pytest.mark.anyio
async def test_create_conversation_uses_existing_write_rate_limit(
    procurement_approval_api_app,
) -> None:
    base_app, runtime, approval, users, password, _request_id, _task_id = procurement_approval_api_app
    runtime.create_conversation = lambda _db, actor_id, title: {
        "id": uuid.uuid4(), "title": title or "新采购对话",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    settings = Settings(
        app_env="production",
        database_url=base_app.state.settings.database_url,
        session_secret="s" * 32,
        model_base_url="https://models.example.test/v1",
        model_api_key="key",
        chat_model="chat",
        embedding_model="embedding",
        hr_confirmation_rate_limit_per_minute=1,
    )
    app = create_app(
        settings,
        session_factory=base_app.state.session_factory,
        procurement_runtime=runtime,
        approval_runtime=approval,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        await _login(client, users["employee"].username, password)
        headers = {"X-CSRF-Token": client.cookies.get("policy_csrf")}
        first = await client.post(
            "/api/v1/procurement/conversations", json={}, headers=headers
        )
        limited = await client.post(
            "/api/v1/procurement/conversations", json={}, headers=headers
        )
    assert first.status_code == 200
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


@pytest.mark.anyio
async def test_conversation_responses_are_closed_and_malformed_values_fail_closed(
    procurement_approval_api_app,
) -> None:
    app, runtime, _approval, users, password, _request_id, _task_id = procurement_approval_api_app
    now = datetime.now(timezone.utc)
    conversation_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    summary = {
        "id": conversation_id, "title": "采购助手",
        "created_at": now, "updated_at": now, "owner_user_id": "secret",
    }
    turn = {
        "id": uuid.uuid4(), "client_turn_id": turn_id, "role": "user",
        "request_content": "我想采购椅子", "text": "请补充申请标题。",
        "blocks": [], "created_at": now,
        "replayed": False, "raw_prompt": "secret",
    }
    runtime.create_conversation = lambda *_args, **_kwargs: dict(summary)
    runtime.list_conversations = lambda *_args, **_kwargs: [dict(summary)]
    runtime.get_conversation = lambda *_args, **_kwargs: {
        **summary, "turns": [{key: value for key, value in turn.items() if key != "replayed"}],
        "internal": "secret",
    }
    runtime.run_turn = lambda *_args, **_kwargs: dict(turn)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _login(client, users["employee"].username, password)
        created = await client.post("/api/v1/procurement/conversations", json={})
        listed = await client.get("/api/v1/procurement/conversations")
        detailed = await client.get(f"/api/v1/procurement/conversations/{conversation_id}")
        turned = await client.post(
            f"/api/v1/procurement/conversations/{conversation_id}/turns",
            json={"client_turn_id": str(turn_id), "text": "查询"},
        )
        runtime.create_conversation = lambda *_args, **_kwargs: {"title": "missing id"}
        malformed = await client.post("/api/v1/procurement/conversations", json={})
    assert set(created.json()) == {"id", "title", "created_at", "updated_at"}
    assert set(listed.json()[0]) == {"id", "title", "created_at", "updated_at"}
    assert set(detailed.json()) == {"id", "title", "created_at", "updated_at", "turns"}
    assert set(detailed.json()["turns"][0]) == {
        "id", "client_turn_id", "role", "request_content", "text", "blocks",
        "created_at"
    }
    assert set(turned.json()) == {
        "id", "client_turn_id", "role", "request_content", "text", "blocks",
        "created_at", "replayed"
    }
    assert turned.json()["request_content"] == "我想采购椅子"
    assert turned.json()["text"] == "请补充申请标题。"
    assert malformed.status_code == 500
    assert malformed.json()["code"] == "api_response_invalid"
