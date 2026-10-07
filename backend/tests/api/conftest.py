from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import uuid

import pytest
from alembic import command
from alembic.config import Config

from policy_api.auth.passwords import hash_password
from policy_api.config import Settings
from policy_api.database import assert_test_database_url
from policy_api.database import create_database_engine, create_session_factory
from policy_api.hr.models import EmployeeProfile
from policy_api.hr.router import HrApiError
from policy_api.main import create_app
from policy_api.procurement.calculation import calculate_total
from policy_api.models import Session as UserSession
from policy_api.models import User, UserRole
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityResolver,
    OrganizationUnit,
    ScopeKind,
)
from policy_api.workbench.audit import SecurityAuditEvent
from policy_api.workbench.events import ProductEvent
from policy_api.workbench.catalog import ModuleCatalog
from policy_api.workbench.runtime import WorkbenchRuntime
from policy_api.tools.errors import ToolError


@pytest.fixture(scope="session")
def TEST_DATABASE_URL() -> str:  # type: ignore[invalid-name]
    database_url = os.getenv("TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for database-backed API tests")
    assert_test_database_url(database_url)
    return database_url


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database(TEST_DATABASE_URL: str) -> None:  # type: ignore[invalid-name]
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    try:
        command.upgrade(config, "head")
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
    yield


class FakeHrRuntime:
    def __init__(self) -> None:
        self.conversations: dict[uuid.UUID, dict[str, object]] = {}
        self.turns: dict[tuple[uuid.UUID, uuid.UUID], tuple[str, dict[str, object]]] = {}
        self.confirmations: dict[uuid.UUID, dict[str, object]] = {}
        self.requests: dict[uuid.UUID, dict[str, object]] = {}
        self.trace_ids: list[tuple[str, str | None]] = []

    def create_conversation(self, _db, actor_id: uuid.UUID, title: str | None):
        now = datetime.now(timezone.utc)
        conversation_id = uuid.uuid4()
        value = {
            "id": conversation_id,
            "owner_user_id": actor_id,
            "title": title or "新 HR 对话",
            "is_archived": False,
            "created_at": now,
            "updated_at": now,
        }
        self.conversations[conversation_id] = value
        return value

    def list_conversations(self, _db, actor_id: uuid.UUID):
        return [
            self._public_conversation(item)
            for item in self.conversations.values()
            if item["owner_user_id"] == actor_id and not item["is_archived"]
        ]

    def get_conversation(self, _db, actor_id: uuid.UUID, conversation_id: uuid.UUID):
        item = self.conversations.get(conversation_id)
        if item is None or item["owner_user_id"] != actor_id or item["is_archived"]:
            raise HrApiError(404, "hr_conversation_not_found", "The conversation was not found.")
        return {
            **self._public_conversation(item),
            "turns": [
                payload
                for (owner_id, _turn_id), (_text, payload) in self.turns.items()
                if owner_id == actor_id
            ],
        }

    def archive_conversation(
        self, _db, actor_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None:
        item = self.conversations.get(conversation_id)
        if (
            item is None
            or item["owner_user_id"] != actor_id
            or item["is_archived"]
        ):
            raise HrApiError(
                404,
                "hr_conversation_not_found",
                "The conversation was not found.",
            )
        item["is_archived"] = True

    def run_turn(
        self,
        _db,
        actor_id: uuid.UUID,
        conversation_id: uuid.UUID,
        client_turn_id: uuid.UUID,
        text: str,
        *,
        request_id: str | None = None,
    ):
        self.trace_ids.append(("run_turn", request_id))
        self.get_conversation(_db, actor_id, conversation_id)
        key = (actor_id, client_turn_id)
        existing = self.turns.get(key)
        if existing is not None:
            if existing[0] != text:
                raise HrApiError(409, "client_turn_id_conflict", "The turn ID belongs to different content.")
            return existing[1]
        payload = {
            "client_turn_id": client_turn_id,
            "text": text,
            "blocks": [{"type": "text", "text": f"已处理：{text}"}],
            "model_calls": 1,
            "read_calls": 0,
            "write_proposals": 0,
        }
        self.turns[key] = (text, payload)
        return payload

    def confirm(
        self, _db, actor_id: uuid.UUID, confirmation_id: uuid.UUID,
        operation_id: uuid.UUID, *, trace_id: str | None = None,
    ):
        self.trace_ids.append(("confirm", trace_id))
        item = self._confirmation(actor_id, confirmation_id)
        if item.get("operation_id") not in {None, operation_id}:
            raise HrApiError(409, "operation_id_conflict", "The operation ID conflicts.")
        item["operation_id"] = operation_id
        item["status"] = "consumed"
        return {
            "type": "execution_result",
            "resource_type": "leave_request",
            "resource_id": item["resource_id"],
            "result": {"status": "pending"},
        }

    def cancel_confirmation(
        self, _db, actor_id: uuid.UUID, confirmation_id: uuid.UUID,
        *, trace_id: str | None = None,
    ):
        self.trace_ids.append(("cancel_confirmation", trace_id))
        item = self._confirmation(actor_id, confirmation_id)
        item["status"] = "cancelled"
        return {"confirmation_id": confirmation_id, "status": "cancelled"}

    def leave_balances(self, _db, actor_id: uuid.UUID, year: int | None):
        return [{
            "leave_type_code": "annual", "leave_type_name": "年假",
            "year": year or 2033, "entitled": "10.00", "used": "2.00",
            "reserved": "1.00", "available": "7.00", "owner": str(actor_id),
        }]

    def list_leave_requests(self, _db, actor_id: uuid.UUID, status: str | None):
        return [
            self._public_request(item)
            for item in self.requests.values()
            if item["owner_user_id"] == actor_id and (status is None or item["status"] == status)
        ]

    def get_leave_request(self, _db, actor_id: uuid.UUID, request_id: uuid.UUID):
        item = self.requests.get(request_id)
        if item is None or item["owner_user_id"] != actor_id:
            raise HrApiError(404, "leave_request_not_found", "The leave request was not found.")
        return self._public_request(item)

    def cancel_intent(
        self, _db, actor_id: uuid.UUID, request_id: uuid.UUID,
        _operation_id: uuid.UUID, *, trace_id: str | None = None,
    ):
        self.trace_ids.append(("cancel_intent", trace_id))
        request = self.get_leave_request(_db, actor_id, request_id)
        confirmation_id = uuid.uuid4()
        self.confirmations[confirmation_id] = {
            "owner_user_id": actor_id,
            "status": "pending",
            "resource_id": request_id,
        }
        return {
            "type": "confirmation",
            "confirmation_id": confirmation_id,
            "tool_name": "hr.cancel_leave_request",
            "preview": request,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        }

    def review_queue(self, _db, _reviewer_id: uuid.UUID, status: str):
        return [
            {
                **self._public_request(item),
                "employee_number": "E-API-001",
                "employee_display_name": "API Test Employee",
            }
            for item in self.requests.values()
            if item["status"] == status
        ]

    def review_detail(
        self,
        _db,
        _reviewer_id: uuid.UUID,
        request_id: uuid.UUID,
    ):
        item = self.requests.get(request_id)
        if item is None:
            raise HrApiError(404, "leave_request_not_found", "The leave request was not found.")
        return {
            **self._public_request(item),
            "employee_number": "E-API-001",
            "employee_display_name": "API Test Employee",
        }

    def approve(
        self, _db, reviewer_id: uuid.UUID, request_id: uuid.UUID,
        _operation_id: uuid.UUID, *, trace_id: str | None = None,
    ):
        self.trace_ids.append(("approve", trace_id))
        item = self.requests.get(request_id)
        if item is None:
            raise HrApiError(404, "leave_request_not_found", "The leave request was not found.")
        item["status"] = "approved"
        item["reviewer_user_id"] = reviewer_id
        return self._public_request(item)

    def reject(
        self, _db, reviewer_id: uuid.UUID, request_id: uuid.UUID,
        _operation_id: uuid.UUID, reason: str, *, trace_id: str | None = None,
    ):
        self.trace_ids.append(("reject", trace_id))
        item = self.requests.get(request_id)
        if item is None:
            raise HrApiError(404, "leave_request_not_found", "The leave request was not found.")
        item["status"] = "rejected"
        item["reviewer_user_id"] = reviewer_id
        item["rejection_reason"] = reason
        return self._public_request(item)

    def seed_request(self, owner_user_id: uuid.UUID) -> uuid.UUID:
        request_id = uuid.uuid4()
        self.requests[request_id] = {
            "id": request_id,
            "owner_user_id": owner_user_id,
            "request_number": f"LR-{uuid.uuid4().hex[:8]}",
            "leave_type_code": "annual",
            "start_date": date(2033, 1, 5),
            "end_date": date(2033, 1, 5),
            "workday_count": Decimal("1.00"),
            "reason": "测试请假",
            "status": "pending",
            "submitted_at": datetime.now(timezone.utc),
            "reviewer_user_id": None,
            "rejection_reason": None,
        }
        return request_id

    def seed_confirmation(self, owner_user_id: uuid.UUID) -> uuid.UUID:
        confirmation_id = uuid.uuid4()
        self.confirmations[confirmation_id] = {
            "owner_user_id": owner_user_id,
            "status": "pending",
            "resource_id": uuid.uuid4(),
        }
        return confirmation_id

    def _confirmation(self, actor_id: uuid.UUID, confirmation_id: uuid.UUID):
        item = self.confirmations.get(confirmation_id)
        if item is None or item["owner_user_id"] != actor_id:
            raise HrApiError(404, "confirmation_not_found", "The confirmation was not found.")
        return item

    @staticmethod
    def _public_conversation(item: dict[str, object]) -> dict[str, object]:
        return {
            key: value
            for key, value in item.items()
            if key not in {"owner_user_id", "is_archived"}
        }

    @staticmethod
    def _public_request(item: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in item.items() if key != "owner_user_id"}


@pytest.fixture
def hr_api_app(TEST_DATABASE_URL: str):  # type: ignore[invalid-name]
    engine = create_database_engine(TEST_DATABASE_URL)
    sessions = create_session_factory(engine)
    suffix = uuid.uuid4().hex
    password = "hr-api-password"
    with sessions() as db:
        users = {
            "employee": User(username=f"hr-employee-{suffix}", password_hash=hash_password(password), role=UserRole.EMPLOYEE),
            "other": User(username=f"hr-other-{suffix}", password_hash=hash_password(password), role=UserRole.EMPLOYEE),
            "hr": User(username=f"hr-reviewer-{suffix}", password_hash=hash_password(password), role=UserRole.HR),
            "reviewer": User(username=f"hr-capability-reviewer-{suffix}", password_hash=hash_password(password), role=UserRole.EMPLOYEE),
            "admin": User(username=f"hr-admin-{suffix}", password_hash=hash_password(password), role=UserRole.ADMIN),
        }
        db.add_all(users.values())
        db.flush()
        db.add(
            CapabilityGrant(
                user_id=users["reviewer"].id,
                capability=Capability.HR_LEAVE_REVIEW.value,
                scope_kind=ScopeKind.GLOBAL.value,
                is_active=True,
            )
        )
        db.commit()
        for user in users.values():
            db.refresh(user)
    runtime = FakeHrRuntime()
    request_id = runtime.seed_request(users["employee"].id)
    settings = Settings(
        app_env="test", database_url=TEST_DATABASE_URL, session_secret="s" * 32,
        model_base_url="https://models.example.test/v1", model_api_key="key",
        chat_model="chat", embedding_model="embedding",
    )
    resolver = CapabilityResolver()
    workbench_runtime = WorkbenchRuntime(
        capability_resolver=resolver,
        module_catalog=ModuleCatalog(resolver),
        hr_runtime=runtime,  # type: ignore[arg-type]
    )
    app = create_app(
        settings,
        session_factory=sessions,
        hr_runtime=runtime,
        workbench_runtime=workbench_runtime,
    )
    yield app, runtime, users, password, request_id
    user_ids = [user.id for user in users.values()]
    with sessions() as db:
        db.query(UserSession).filter(UserSession.user_id.in_(user_ids)).delete(
            synchronize_session=False
        )
        db.query(CapabilityGrant).filter(
            CapabilityGrant.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(user_ids)).delete(
            synchronize_session=False
        )
        db.commit()
    engine.dispose()


@pytest.fixture
def workbench_api_app(TEST_DATABASE_URL: str):  # type: ignore[invalid-name]
    engine = create_database_engine(TEST_DATABASE_URL)
    sessions = create_session_factory(engine)
    suffix = uuid.uuid4().hex
    password = "workbench-api-password"
    employee_username = f"workbench-employee-{suffix}"
    hr_username = f"workbench-hr-{suffix}"
    admin_username = f"workbench-admin-{suffix}"
    with sessions() as db:
        employee = User(
            username=employee_username,
            password_hash=hash_password(password),
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        admin = User(
            username=admin_username,
            password_hash=hash_password(password),
            role=UserRole.ADMIN,
            is_active=True,
        )
        hr = User(
            username=hr_username,
            password_hash=hash_password(password),
            role=UserRole.HR,
            is_active=True,
        )
        db.add_all([employee, hr, admin])
        db.flush()
        db.add_all(
            [
                EmployeeProfile(
                    user_id=employee.id,
                    employee_number=f"WB-{suffix}",
                    display_name="Workbench Employee",
                    hire_date=date(2024, 1, 1),
                    is_active=True,
                ),
                EmployeeProfile(
                    user_id=hr.id,
                    employee_number=f"WB-HR-{suffix}",
                    display_name="Workbench HR",
                    hire_date=date(2024, 1, 1),
                    is_active=True,
                ),
            ]
        )
        db.add_all(
            [
                CapabilityGrant(
                    user_id=admin.id,
                    capability=capability.value,
                    scope_kind=ScopeKind.GLOBAL.value,
                    is_active=True,
                )
                for capability in (
                    Capability.KNOWLEDGE_MANAGE,
                    Capability.ORGANIZATION_MANAGE,
                    Capability.ANALYTICS_VIEW,
                )
            ]
        )
        db.commit()
        user_ids = (employee.id, hr.id, admin.id)

    resolver = CapabilityResolver()
    runtime = WorkbenchRuntime(
        capability_resolver=resolver,
        module_catalog=ModuleCatalog(resolver),
    )
    settings = Settings(
        app_env="test",
        database_url=TEST_DATABASE_URL,
        session_secret="s" * 32,
        model_base_url="https://models.example.test/v1",
        model_api_key="key",
        chat_model="chat",
        embedding_model="embedding",
    )
    app = create_app(
        settings,
        session_factory=sessions,
        workbench_runtime=runtime,
    )
    yield app, runtime, {
        "employee": employee_username,
        "hr": hr_username,
        "admin": admin_username,
        "password": password,
        "suffix": suffix,
    }
    with sessions() as db:
        db.query(UserSession).filter(UserSession.user_id.in_(user_ids)).delete(
            synchronize_session=False
        )
        db.query(ProductEvent).filter(
            ProductEvent.actor_user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        db.query(SecurityAuditEvent).filter(
            SecurityAuditEvent.actor_user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        db.query(CapabilityGrant).filter(
            CapabilityGrant.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        db.query(EmployeeProfile).filter(
            EmployeeProfile.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        organization_query = db.query(OrganizationUnit).filter(
            OrganizationUnit.code.like(f"WBORG-{suffix.upper()}-%")
        )
        organization_query.update(
            {OrganizationUnit.parent_id: None},
            synchronize_session=False,
        )
        organization_query.delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(user_ids)).delete(
            synchronize_session=False
        )
        db.commit()
        assert organization_query.count() == 0
    engine.dispose()


class FakeProcurementApiRuntime:
    def __init__(self) -> None:
        self.requests: dict[uuid.UUID, dict[str, object]] = {}
        self.operations: dict[tuple[uuid.UUID, uuid.UUID], tuple[dict[str, object], uuid.UUID]] = {}
        self.withdraw_operations: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {}
        self.withdraw_mutations: dict[uuid.UUID, int] = {}
        self.trace_ids: list[str | None] = []
        self.preview_calls: list[object] = []

    def preview_request(self, request_input):
        self.preview_calls.append(request_input)
        totals = calculate_total(request_input.items)
        return {
            "currency": "CNY",
            "subtotals": [format(value, ".2f") for value in totals.subtotals],
            "total": format(totals.total_amount, ".2f"),
        }

    def submit_request(self, _db, *, actor, client_operation_id, request_input, request_id):
        self.trace_ids.append(request_id)
        payload = request_input.model_dump(mode="json")
        key = (actor.id, client_operation_id)
        existing = self.operations.get(key)
        if existing is not None:
            original, resource_id = existing
            if original != payload:
                raise ToolError("operation_id_conflict")
            return self._submission(self.requests[resource_id], replayed=True)
        resource_id = uuid.uuid4()
        value = {
            "id": resource_id,
            "request_number": f"PR-{len(self.requests) + 1:04d}",
            "title": payload["title"],
            "total": "20.00",
            "status": "pending_manager",
            "submitted_at": datetime.now(timezone.utc),
            "owner_user_id": actor.id,
            "purpose": payload["purpose"],
            "needed_by_date": payload["needed_by_date"],
            "currency": payload["currency"],
            "items": payload["items"],
        }
        self.requests[resource_id] = value
        self.operations[key] = (payload, resource_id)
        return self._submission(value, replayed=False)

    def list_requests(self, _db, *, actor, status, submitted_from, submitted_to, offset, limit):
        if submitted_from is not None and submitted_to is not None and submitted_from > submitted_to:
            raise ValueError("date_range_invalid")
        values = [
            self._summary(value)
            for value in self.requests.values()
            if value["owner_user_id"] == actor.id
            and (status is None or value["status"] == status)
            and (submitted_from is None or value["submitted_at"].date() >= submitted_from)
            and (submitted_to is None or value["submitted_at"].date() <= submitted_to)
        ]
        return {"items": values[offset : offset + limit], "offset": offset, "limit": limit, "total": len(values)}

    def get_request(self, _db, *, actor, request_id):
        value = self.requests.get(request_id)
        if value is None or value["owner_user_id"] != actor.id:
            raise ToolError("procurement_request_not_found")
        return self._detail(value)

    def withdraw_request(self, _db, *, actor, request_id, client_operation_id):
        value = self.requests.get(request_id)
        if value is None or value["owner_user_id"] != actor.id:
            raise ToolError("procurement_request_not_found")
        key = (actor.id, client_operation_id)
        existing = self.withdraw_operations.get(key)
        if existing is not None:
            if existing != request_id:
                raise ToolError("approval_operation_id_conflict")
            return self._withdrawal(value, replayed=True)
        if value["status"] != "pending_manager":
            raise ToolError("approval_instance_state_conflict")
        self.withdraw_operations[key] = request_id
        value["status"] = "cancelled"
        self.withdraw_mutations[request_id] = self.withdraw_mutations.get(request_id, 0) + 1
        return self._withdrawal(value, replayed=False)

    def seed(
        self,
        owner_user_id: uuid.UUID,
        *,
        status: str = "pending_manager",
        submitted_at: datetime | None = None,
    ) -> uuid.UUID:
        request_id = uuid.uuid4()
        self.requests[request_id] = {
            "id": request_id,
            "request_number": f"PR-{len(self.requests) + 1:04d}",
            "title": "测试采购",
            "total": "20.00",
            "status": status,
            "submitted_at": submitted_at or datetime.now(timezone.utc),
            "owner_user_id": owner_user_id,
            "purpose": "API 测试",
            "needed_by_date": "2035-01-01",
            "currency": "CNY",
            "items": [{
                "category": "office_supplies", "name": "签字笔",
                "specification": None, "quantity": "2", "unit": "盒",
                "unit_price": "10.00", "subtotal": "20.00",
            }],
        }
        return request_id

    @staticmethod
    def _summary(value: dict[str, object]) -> dict[str, object]:
        return {key: value[key] for key in ("id", "request_number", "title", "total", "status", "submitted_at")}

    @classmethod
    def _submission(cls, value: dict[str, object], *, replayed: bool) -> dict[str, object]:
        return {**cls._summary(value), "replayed": replayed}

    @classmethod
    def _detail(cls, value: dict[str, object]) -> dict[str, object]:
        return {
            "id": value["id"],
            "summary": {key: value[key] for key in ("request_number", "title", "total", "status")},
            "purpose": value["purpose"], "needed_by_date": value["needed_by_date"],
            "currency": value["currency"], "items": value["items"],
            "applicant": {"display_name": "API Test Employee"},
            "organization": {"display_name": "API Test Organization"},
            "timeline": [],
        }

    @staticmethod
    def _withdrawal(value: dict[str, object], *, replayed: bool) -> dict[str, object]:
        return {
            "instance_id": value["id"], "status": value["status"],
            "current_step_key": None, "replayed": replayed,
        }


class FakeApprovalApiRuntime:
    def __init__(self, reviewer_id: uuid.UUID) -> None:
        self.reviewer_id = reviewer_id
        self.tasks: dict[uuid.UUID, dict[str, object]] = {}
        self.operations: dict[tuple[uuid.UUID, uuid.UUID], tuple[str, str | None, uuid.UUID]] = {}
        self.action_users: dict[uuid.UUID, set[uuid.UUID]] = {}
        self.known_action_users: dict[uuid.UUID, set[uuid.UUID]] = {}
        self.list_calls: list[dict[str, object]] = []
        self.mutations: dict[uuid.UUID, int] = {}

    def list_tasks(self, _db, *, actor, status, process_key, activated_from, activated_to, offset, limit):
        self.list_calls.append({
            "status": status, "process_key": process_key,
            "activated_from": activated_from, "activated_to": activated_to,
            "offset": offset, "limit": limit,
        })
        if activated_from is not None and activated_to is not None and activated_from > activated_to:
            raise ValueError("date_range_invalid")
        values = [value["public"] for task_id, value in self.tasks.items()
            if actor.id in value["view_users"]
            and (status is None or value["public"]["status"] == status)
            and (process_key is None or value["public"]["process_key"] == process_key)
            and (activated_from is None or value["public"]["activated_at"] >= activated_from)
            and (activated_to is None or value["public"]["activated_at"] <= activated_to)]
        return {"items": values[offset:offset + limit], "offset": offset, "limit": limit, "total": len(values)}

    def get_task(self, _db, *, actor, task_id):
        value = self.tasks.get(task_id)
        if value is None or actor.id not in value["view_users"]:
            raise ToolError("approval_task_not_found")
        return {"task": value["public"], "subject": value["subject"]}

    def approve(self, _db, *, actor, task_id, client_operation_id, comment):
        return self._decide(actor, task_id, client_operation_id, "approved", comment)

    def reject(self, _db, *, actor, task_id, client_operation_id, reason):
        return self._decide(actor, task_id, client_operation_id, "rejected", reason)

    def _decide(self, actor, task_id, client_operation_id, status, comment):
        value = self.tasks.get(task_id)
        if value is None:
            raise ToolError("approval_task_not_found")
        key = (actor.id, client_operation_id)
        signature = (status, comment, task_id)
        if key in self.operations:
            if self.operations[key] != signature:
                raise ToolError("approval_operation_id_conflict")
            return self._transition(value, replayed=True)
        if actor.id not in value["view_users"]:
            if actor.id in self.known_action_users[task_id]:
                raise ToolError("approval_capability_required")
            raise ToolError("approval_task_not_found")
        if actor.id not in self.action_users[task_id]:
            raise ToolError("approval_capability_required")
        if value["public"]["status"] != "pending":
            raise ToolError("approval_task_state_conflict")
        self.operations[key] = signature
        value["public"]["status"] = status
        self.mutations[task_id] = self.mutations.get(task_id, 0) + 1
        return self._transition(value, replayed=False)

    def seed(
        self,
        *,
        status: str = "pending",
        process_key: str = "procurement.request",
        activated_at: datetime | None = None,
        view_users: set[uuid.UUID] | None = None,
    ) -> uuid.UUID:
        task_id = uuid.uuid4()
        public = {
            "task_id": task_id,
            "instance_id": uuid.uuid4(),
            "process_key": process_key,
            "subject_type": "procurement_request",
            "step_key": "department_manager_review",
            "step_label": "部门负责人审批",
            "status": status,
            "submitted_at": datetime.now(timezone.utc),
            "activated_at": activated_at or datetime.now(timezone.utc),
            "completed_at": None,
            "subject": {"request_number": "PR-0001", "title": "测试", "total": "20.00", "status": "pending_manager"},
        }
        self.tasks[task_id] = {
            "public": public,
            "view_users": set(view_users or {self.reviewer_id}),
            "subject": {
                "summary": public["subject"], "purpose": "API 测试",
                "needed_by_date": "2035-01-01", "currency": "CNY",
                "items": [], "applicant": {"display_name": "API Test Employee"},
                "organization": {"display_name": "API Test Organization"},
                "timeline": [],
            },
        }
        self.action_users[task_id] = set(self.tasks[task_id]["view_users"])
        self.known_action_users[task_id] = set(self.tasks[task_id]["view_users"])
        return task_id

    def revoke_action(self, task_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        self.action_users[task_id].discard(actor_id)
        self.tasks[task_id]["view_users"].discard(actor_id)

    @staticmethod
    def _transition(value: dict[str, object], *, replayed: bool) -> dict[str, object]:
        public = value["public"]
        return {
            "instance_id": public["instance_id"], "status": public["status"],
            "current_step_key": None, "replayed": replayed,
        }


@pytest.fixture
def procurement_approval_api_app(TEST_DATABASE_URL: str):  # type: ignore[invalid-name]
    engine = create_database_engine(TEST_DATABASE_URL)
    sessions = create_session_factory(engine)
    suffix = uuid.uuid4().hex
    password = "procurement-api-password"
    with sessions() as db:
        users = {
            "employee": User(username=f"pa-employee-{suffix}", password_hash=hash_password(password), role=UserRole.EMPLOYEE),
            "other": User(username=f"pa-other-{suffix}", password_hash=hash_password(password), role=UserRole.EMPLOYEE),
            "reviewer": User(username=f"pa-reviewer-{suffix}", password_hash=hash_password(password), role=UserRole.EMPLOYEE),
            "hr": User(username=f"pa-hr-{suffix}", password_hash=hash_password(password), role=UserRole.HR),
            "admin": User(username=f"pa-admin-{suffix}", password_hash=hash_password(password), role=UserRole.ADMIN),
        }
        db.add_all(users.values())
        db.commit()
        for user in users.values():
            db.refresh(user)
    procurement_runtime = FakeProcurementApiRuntime()
    approval_runtime = FakeApprovalApiRuntime(users["reviewer"].id)
    request_id = procurement_runtime.seed(users["employee"].id)
    task_id = approval_runtime.seed()
    settings = Settings(
        app_env="test", database_url=TEST_DATABASE_URL, session_secret="s" * 32,
        model_base_url="https://models.example.test/v1", model_api_key="key",
        chat_model="chat", embedding_model="embedding", hr_confirmation_rate_limit_per_minute=100,
    )
    app = create_app(
        settings,
        session_factory=sessions,
        procurement_runtime=procurement_runtime,
        approval_runtime=approval_runtime,
    )
    yield app, procurement_runtime, approval_runtime, users, password, request_id, task_id
    user_ids = [user.id for user in users.values()]
    with sessions() as db:
        db.query(UserSession).filter(UserSession.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        db.commit()
    engine.dispose()
