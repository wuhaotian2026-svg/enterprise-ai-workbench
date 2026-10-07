from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from policy_api.assistant_drafts.models import AssistantFlowDraft, DraftStatus
from policy_api.assistant_drafts.store import AssistantDraftStore
from policy_api.hr.runtime import HrRuntime
from policy_api.hr.schemas import HrDomainError
from policy_api.hr.enums import LeaveRequestStatus, LeaveTypeCode, WorkCalendarDayKind
from policy_api.hr.models import (
    EmployeeProfile,
    HrConversation,
    HrTurn,
    LeaveAccount,
    LeaveAccountEvent,
    LeaveRequest,
    LeaveType,
    WorkCalendarDay,
)
from policy_api.knowledge.tools import PolicySearchOutcome
from policy_api.models import User, UserRole
from policy_api.slot_extraction.fingerprint import SlotExtractionFingerprinter
from policy_api.slot_extraction.repository import SlotExtractionOperationRepository
from policy_api.slot_extraction.schemas import SlotExtractionEnvelope
from policy_api.slot_extraction.service import SlotExtractionService
from policy_api.tools.planner_client import PlannerError
from policy_api.tools.types import PlannedToolCall, PlannerTurn
from policy_api.tools.audit import AuditSummary
from policy_api.tools.confirmation import canonical_arguments_hash, create_tool_confirmation
from policy_api.tools.enums import (
    ToolAuditEventKind,
    ToolConfirmationStatus,
    ToolInvocationStatus,
)
from policy_api.tools.errors import ToolError
from policy_api.tools.definitions import ToolContext
from policy_api.tools.models import ToolAuditEvent, ToolConfirmation, ToolInvocation
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityResolver,
    OrganizationUnit,
    ScopeKind,
)
from policy_api.workbench.events import ProductEvent, ProductEventValidationError
from policy_api.workbench.audit import SecurityAuditEvent


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


class DirectTextPlanner:
    model = "test-planner"

    def __init__(self) -> None:
        self.closed = 0

    def complete(  # type: ignore[no-untyped-def]
        self, messages, *, tools, required_tool_name=None
    ):
        del messages, tools, required_tool_name
        return PlannerTurn(text="已处理", tool_calls=())

    def close(self) -> None:
        self.closed += 1


class ScriptedPlanner:
    def __init__(self, turns: list[PlannerTurn | Exception]) -> None:
        self.turns = list(turns)
        self.tools_seen: list[list[dict[str, object]]] = []

    def complete(  # type: ignore[no-untyped-def]
        self, _messages, *, tools, required_tool_name=None
    ):
        del required_tool_name
        self.tools_seen.append(list(tools))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class CapturingTextPlanner(DirectTextPlanner):
    def __init__(self) -> None:
        super().__init__()
        self.tools_seen: list[list[dict[str, object]]] = []

    def complete(  # type: ignore[no-untyped-def]
        self, messages, *, tools, required_tool_name=None
    ):
        del messages, required_tool_name
        self.tools_seen.append(list(tools))
        return PlannerTurn(text="制度答复", tool_calls=())


class EmptySlotClient:
    model = "test-empty-slot-extractor"

    def extract(  # type: ignore[no-untyped-def]
        self, *, current_user_turn_text, slot_schema
    ) -> SlotExtractionEnvelope:
        del current_user_turn_text
        return SlotExtractionEnvelope(
            schema_version=slot_schema.version,
            candidates=[],
        )


def empty_slot_service() -> SlotExtractionService:
    return SlotExtractionService(
        client=EmptySlotClient(),
        repository=SlotExtractionOperationRepository(),
        fingerprinter=SlotExtractionFingerprinter(b"test-empty-slot-secret" * 2),
        model_timeout_seconds=30,
    )


def planned_tool(
    call_id: str, name: str, arguments: dict[str, object]
) -> PlannedToolCall:
    return PlannedToolCall(call_id=call_id, name=name, arguments=arguments)


def test_runtime_orchestrator_uses_hr_flow_policy_without_database_access() -> None:
    planner = CapturingTextPlanner()
    runtime = HrRuntime(planner=planner)  # type: ignore[arg-type]
    orchestrator = runtime._orchestrator(None, None)  # type: ignore[arg-type]

    response = orchestrator.run(
        "公司年假结转有什么规定？",
        ToolContext(actor_user_id=uuid.uuid4(), role=UserRole.EMPLOYEE),
    )

    assert response.blocks[-1].text == "制度答复"
    assert [
        tool["function"]["name"] for tool in planner.tools_seen[0]
    ] == ["knowledge_search_policy"]


@pytest.fixture
def runtime_session() -> tuple[Session, dict[str, object]]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for HR runtime integration tests")
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    command.upgrade(config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    suffix = uuid.uuid4().hex
    try:
        owner = User(
            username=f"runtime-owner-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        other = User(
            username=f"runtime-other-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        session.add_all([owner, other])
        session.flush()
        organization = OrganizationUnit(
            code=f"RUNTIME-{suffix.upper()}", name="Runtime Organization",
            is_active=True,
        )
        session.add(organization)
        session.flush()
        employee = EmployeeProfile(
            user_id=owner.id,
            employee_number=f"R-{suffix}",
            display_name="Runtime Owner",
            hire_date=date(2024, 1, 1),
            organization_unit_id=organization.id,
            is_active=True,
        )
        annual = LeaveType(
            code=LeaveTypeCode.ANNUAL,
            display_name="Annual leave",
            is_enabled=True,
        )
        session.add_all([employee, annual])
        session.flush()
        account = LeaveAccount(
            employee_id=employee.id,
            leave_type_id=annual.id,
            year=2033,
            entitled=Decimal("10.00"),
            used=Decimal("0.00"),
            reserved=Decimal("1.00"),
            version=1,
        )
        request = LeaveRequest(
            request_number=f"LR-RUNTIME-{suffix[:20]}",
            employee_id=employee.id,
            leave_type_id=annual.id,
            start_date=date(2033, 1, 5),
            end_date=date(2033, 1, 5),
            workday_count=Decimal("1.00"),
            reason="Runtime test",
            status=LeaveRequestStatus.PENDING,
            submitted_at=datetime.now(timezone.utc),
        )
        session.add_all([account, request])
        session.flush()
        yield session, {
            "owner": owner,
            "other": other,
            "request_id": request.id,
            "organization_id": organization.id,
        }
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        engine.dispose()
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original


def test_runtime_persists_owner_scoped_idempotent_conversation_turns(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    other = values["other"]
    assert isinstance(owner, User) and isinstance(other, User)
    planner = DirectTextPlanner()
    runtime = HrRuntime(
        planner=planner,  # type: ignore[arg-type]
        slot_extraction=empty_slot_service(),
        search_policy=lambda _db, _query: PolicySearchOutcome(
            status="refused", text=None, refusal_reason="insufficient_evidence", citations=()
        ),
    )

    conversation = runtime.create_conversation(db, owner.id, " 请假咨询 ")
    assert conversation["title"] == "请假咨询"
    assert runtime.list_conversations(db, owner.id)[0]["id"] == conversation["id"]
    assert runtime.list_conversations(db, other.id) == []

    client_turn_id = uuid.uuid4()
    first = runtime.run_turn(
        db, owner.id, conversation["id"], client_turn_id, "查询余额"
    )
    replay = runtime.run_turn(
        db, owner.id, conversation["id"], client_turn_id, "查询余额"
    )
    assert first == replay
    assert first["blocks"] == [{"type": "text", "text": "已处理"}]

    with pytest.raises(HrDomainError, match="client_turn_id_conflict"):
        runtime.run_turn(
            db, owner.id, conversation["id"], client_turn_id, "不同文本"
        )
    with pytest.raises(HrDomainError, match="hr_conversation_not_found"):
        runtime.get_conversation(db, other.id, conversation["id"])

    detail = runtime.get_conversation(db, owner.id, conversation["id"])
    assert detail["turns"][0]["client_turn_id"] == client_turn_id
    assert detail["turns"][0]["text"] == "查询余额"
    assert detail["turns"][0]["created_at"] <= datetime.now(timezone.utc)


def test_archive_conversation_closes_only_its_draft_and_preserves_business_records(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    other = values["other"]
    assert isinstance(owner, User) and isinstance(other, User)
    runtime = HrRuntime(
        planner=DirectTextPlanner(),  # type: ignore[arg-type]
        slot_extraction=empty_slot_service(),
    )
    archived = runtime.create_conversation(db, owner.id, "待归档")
    retained = runtime.create_conversation(db, owner.id, "保留")
    runtime.run_turn(
        db,
        owner.id,
        archived["id"],
        uuid.uuid4(),
        "查询余额",
        request_id="archive-preservation",
    )
    runtime.draft_store.save(
        db,
        owner_user_id=owner.id,
        module_key="hr",
        conversation_id=archived["id"],
        intent="submit_leave_request",
        fields={"reason": "家庭事务"},
        sources={"reason": "家庭事务"},
        pending={},
    )
    runtime.draft_store.save(
        db,
        owner_user_id=owner.id,
        module_key="hr",
        conversation_id=retained["id"],
        intent="submit_leave_request",
        fields={"reason": "保留事务"},
        sources={"reason": "保留事务"},
        pending={},
    )
    audit = SecurityAuditEvent(
        event_name="test_archive_preservation",
        actor_user_id=owner.id,
        target_type="hr_conversation",
        target_id=archived["id"],
        operation_id=uuid.uuid4(),
        outcome="recorded",
        request_id="archive-preservation",
        summary={},
    )
    db.add(audit)
    db.flush()
    db.commit()
    baseline = {
        "leave_requests": db.scalar(select(func.count()).select_from(LeaveRequest)),
        "turns": db.scalar(select(func.count()).select_from(HrTurn)),
        "events": db.scalar(select(func.count()).select_from(ProductEvent)),
        "audits": db.scalar(select(func.count()).select_from(SecurityAuditEvent)),
    }

    with pytest.raises(HrDomainError, match="hr_conversation_not_found"):
        runtime.archive_conversation(db, other.id, archived["id"])
    runtime.archive_conversation(db, owner.id, archived["id"])

    archived_row = db.get(HrConversation, archived["id"])
    assert archived_row is not None and archived_row.is_archived is True
    assert [item["id"] for item in runtime.list_conversations(db, owner.id)] == [
        retained["id"]
    ]
    with pytest.raises(HrDomainError, match="hr_conversation_not_found"):
        runtime.get_conversation(db, owner.id, archived["id"])
    drafts = db.scalars(
        select(AssistantFlowDraft).where(
            AssistantFlowDraft.owner_user_id == owner.id
        )
    ).all()
    statuses = {item.conversation_id: item.status for item in drafts}
    assert statuses[archived["id"]] == DraftStatus.CLOSED.value
    assert statuses[retained["id"]] == DraftStatus.ACTIVE.value
    assert db.get(HrConversation, retained["id"]) is not None
    assert db.get(SecurityAuditEvent, audit.id) is not None
    assert {
        "leave_requests": db.scalar(select(func.count()).select_from(LeaveRequest)),
        "turns": db.scalar(select(func.count()).select_from(HrTurn)),
        "events": db.scalar(select(func.count()).select_from(ProductEvent)),
        "audits": db.scalar(select(func.count()).select_from(SecurityAuditEvent)),
    } == baseline


class FailingArchiveDraftStore(AssistantDraftStore):
    def transition(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise RuntimeError("draft_transition_failed")


def test_archive_conversation_rolls_back_when_draft_close_fails(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    creator = HrRuntime(
        planner=DirectTextPlanner(),  # type: ignore[arg-type]
        slot_extraction=empty_slot_service(),
    )
    conversation = creator.create_conversation(db, owner.id, "事务回滚")
    failing = HrRuntime(
        planner=DirectTextPlanner(),  # type: ignore[arg-type]
        slot_extraction=empty_slot_service(),
        draft_store=FailingArchiveDraftStore(),
    )

    with pytest.raises(RuntimeError, match="draft_transition_failed"):
        failing.archive_conversation(db, owner.id, conversation["id"])

    db.expire_all()
    row = db.get(HrConversation, conversation["id"])
    assert row is not None and row.is_archived is False


def test_conversation_history_projects_authoritative_confirmation_states_read_only(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    other = values["other"]
    request_id = values["request_id"]
    assert isinstance(owner, User) and isinstance(other, User)
    assert isinstance(request_id, uuid.UUID)
    runtime = HrRuntime(
        planner=DirectTextPlanner(),  # type: ignore[arg-type]
        slot_extraction=empty_slot_service(),
    )
    conversation = runtime.create_conversation(db, owner.id, "确认状态历史")
    turn = HrTurn(
        id=uuid.uuid4(),
        conversation_id=conversation["id"],
        owner_user_id=owner.id,
        client_turn_id=uuid.uuid4(),
        role="assistant",
        content="提交请假申请",
        blocks={},  # type: ignore[arg-type]
        model_name="test-planner",
    )
    db.add(turn)
    db.flush()
    now = datetime.now(timezone.utc)

    def confirmation(
        suffix: str,
        *,
        expires_at: datetime,
        status: ToolConfirmationStatus = ToolConfirmationStatus.PENDING,
        result_resource_id: uuid.UUID | None = None,
    ) -> ToolConfirmation:
        arguments = {
            "leave_type_code": "annual",
            "start_date": "2033-01-05",
            "end_date": "2033-01-05",
            "reason": f"private-{suffix}",
        }
        invocation = ToolInvocation(
            conversation_id=conversation["id"],
            turn_id=turn.id,
            actor_user_id=owner.id,
            provider_call_id=f"history-{suffix}",
            tool_name="hr.submit_leave_request",
            provider_tool_name="hr_submit_leave_request",
            risk_level="write",
            status=ToolInvocationStatus.PROPOSED,
            arguments_hash=canonical_arguments_hash(arguments),
        )
        db.add(invocation)
        db.flush()
        created = create_tool_confirmation(
            db,
            invocation=invocation,
            owner_user_id=owner.id,
            normalized_arguments=arguments,
            preview={"reason": suffix},
            expires_at=expires_at,
            audit_summary=AuditSummary.for_execution(
                tool_name=invocation.tool_name,
                risk_level="write",
                outcome="pending",
            ),
        )
        if status == ToolConfirmationStatus.CANCELLED:
            created.status = status
            created.cancelled_at = now
        elif status == ToolConfirmationStatus.EXPIRED:
            created.status = status
        elif status == ToolConfirmationStatus.CONSUMED:
            assert result_resource_id is not None
            created.status = status
            created.consumed_at = now
            created.client_operation_id = uuid.uuid4()
            created.result_resource_type = "leave_request"
            created.result_resource_id = result_resource_id
            invocation.status = ToolInvocationStatus.SUCCEEDED
            invocation.result_resource_type = "leave_request"
            invocation.result_resource_id = result_resource_id
        return created

    pending = confirmation(
        "pending", expires_at=now + timedelta(minutes=10)
    )
    elapsed_pending = confirmation(
        "elapsed", expires_at=now - timedelta(minutes=1)
    )
    cancelled = confirmation(
        "cancelled",
        expires_at=now + timedelta(minutes=10),
        status=ToolConfirmationStatus.CANCELLED,
    )
    expired = confirmation(
        "expired",
        expires_at=now - timedelta(minutes=1),
        status=ToolConfirmationStatus.EXPIRED,
    )
    consumed = confirmation(
        "consumed",
        expires_at=now + timedelta(minutes=10),
        status=ToolConfirmationStatus.CONSUMED,
        result_resource_id=request_id,
    )
    snapshots = [pending, elapsed_pending, cancelled, expired, consumed]
    turn.blocks = {  # type: ignore[assignment]
        "blocks": [
            {
                "type": "confirmation",
                "confirmation_id": str(item.id),
                "tool_name": item.tool_name,
                "preview": dict(item.preview),
                "expires_at": item.expires_at.isoformat(),
            }
            for item in snapshots
        ],
        "model_calls": 1,
        "read_calls": 0,
        "write_proposals": 5,
        "slot_extraction_calls": 1,
    }
    db.commit()
    tracked_models = (
        LeaveRequest,
        ToolInvocation,
        ToolConfirmation,
        ToolAuditEvent,
        SecurityAuditEvent,
        ProductEvent,
    )
    baseline = {
        model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in tracked_models
    }

    first = runtime.get_conversation(db, owner.id, conversation["id"])
    second = runtime.get_conversation(db, owner.id, conversation["id"])

    assert first == second
    blocks = first["turns"][0]["blocks"]
    assert [block["type"] for block in blocks] == [
        "confirmation",
        "confirmation_terminal",
        "confirmation_terminal",
        "confirmation_terminal",
        "execution_result",
    ]
    assert blocks[0]["confirmation_id"] == str(pending.id)
    assert [blocks[index]["status"] for index in (1, 2, 3)] == [
        "expired",
        "cancelled",
        "expired",
    ]
    assert blocks[4]["resource_type"] == "leave_request"
    assert blocks[4]["resource_id"] == request_id
    assert blocks[4]["result"]["request_number"].startswith("LR-RUNTIME-")
    assert blocks[4]["result"]["status"] == "pending"
    assert "normalized_arguments" not in str(first)
    assert {
        model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in tracked_models
    } == baseline
    with pytest.raises(HrDomainError, match="hr_conversation_not_found"):
        runtime.get_conversation(db, other.id, conversation["id"])


def test_policy_clarification_is_respond_only_with_zero_write_proposals(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    planner = ScriptedPlanner(
        [
            PlannerTurn(
                None,
                (
                    planned_tool(
                        "policy-clarification-read",
                        "knowledge_search_policy",
                        {"query": "南京出差标准"},
                    ),
                ),
            ),
            PlannerTurn("请补充城市档位和住宿晚数。", ()),
        ]
    )
    runtime = HrRuntime(
        planner=planner,  # type: ignore[arg-type]
        slot_extraction=empty_slot_service(),
        search_policy=lambda _db, _query: PolicySearchOutcome(
            status="needs_clarification",
            text="其他城市住宿标准为350元每晚。",
            refusal_reason=None,
            citations=(),
            clarification_questions=(
                "适用哪一城市档位？",
                "实际住宿几晚？",
            ),
        ),
    )
    baseline_requests = db.scalar(select(func.count()).select_from(LeaveRequest))
    conversation = runtime.create_conversation(db, owner.id, "policy clarification")

    response = runtime.run_turn(
        db,
        owner.id,
        conversation["id"],
        uuid.uuid4(),
        "南京出差制度标准是什么？",
    )

    assert planner.tools_seen[0]
    assert planner.tools_seen[1] == []
    assert {
        "type": "policy_clarification",
        "questions": ["适用哪一城市档位？", "实际住宿几晚？"],
    } in response["blocks"]
    assert db.scalar(select(func.count()).select_from(ToolConfirmation)) == 0
    assert db.scalar(
        select(func.count())
        .select_from(ToolInvocation)
        .where(ToolInvocation.risk_level == "write")
    ) == 0
    assert db.scalar(select(func.count()).select_from(LeaveRequest)) == baseline_requests


def test_runtime_records_low_sensitivity_turn_and_intent_events(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    runtime = HrRuntime(
        planner=DirectTextPlanner(),  # type: ignore[arg-type]
        slot_extraction=empty_slot_service(),
    )
    conversation = runtime.create_conversation(db, owner.id, "事件测试")

    runtime.run_turn(
        db, owner.id, conversation["id"], uuid.uuid4(), "这是不能进入事件的正文",
        request_id="hr/non-uuid/trace",
    )

    events = db.scalars(select(ProductEvent).where(
        ProductEvent.actor_user_id == owner.id).order_by(ProductEvent.created_at)).all()
    assert [event.event_name for event in events] == [
        "hr_turn_submitted", "hr_intent_resolved",
    ]
    assert events[0].request_id == "hr/non-uuid/trace"
    assert all(event.organization_unit_id == values["organization_id"] for event in events)
    assert events[0].dimensions == {"message_length_bucket": "0_50"}
    assert events[1].dimensions == {
        "intent": "unknown", "clarification_required": False,
    }
    assert "这是不能进入事件的正文" not in repr([
        event.dimensions for event in events
    ])


def test_runtime_records_read_validation_and_provider_error_lifecycle_events(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)

    def event_rows(trace_id: str) -> list[ProductEvent]:
        return list(
            db.scalars(
                select(ProductEvent)
                .where(ProductEvent.request_id == trace_id)
                .order_by(ProductEvent.created_at, ProductEvent.id)
            ).all()
        )

    read_planner = ScriptedPlanner(
        [
            PlannerTurn(
                None,
                (
                    planned_tool(
                        "read-1",
                        "knowledge_search_policy",
                        {"query": "年假制度"},
                    ),
                ),
            ),
            PlannerTurn("已检索", ()),
        ]
    )
    read_runtime = HrRuntime(
        planner=read_planner,  # type: ignore[arg-type]
        slot_extraction=empty_slot_service(),
        search_policy=lambda _db, _query: PolicySearchOutcome(
            status="refused",
            text=None,
            refusal_reason="insufficient_evidence",
            citations=(),
        ),
    )
    conversation = read_runtime.create_conversation(db, owner.id, "read events")
    read_runtime.run_turn(
        db,
        owner.id,
        conversation["id"],
        uuid.uuid4(),
        "查询年假制度",
        request_id="lifecycle-read",
    )
    read_events = event_rows("lifecycle-read")
    assert [event.event_name for event in read_events] == [
        "hr_turn_submitted",
        "hr_intent_resolved",
        "tool_planned",
        "tool_read_succeeded",
    ]
    assert read_events[1].dimensions == {
        "intent": "get_leave_policy",
        "clarification_required": False,
    }
    assert read_events[2].dimensions == {
        "tool_name": "knowledge.search_policy",
        "risk_level": "read",
    }
    assert read_planner.tools_seen[0]
    assert read_planner.tools_seen[1] == []

    invalid_runtime = HrRuntime(
        planner=ScriptedPlanner(  # type: ignore[arg-type]
            [
                PlannerTurn(
                    None,
                    (planned_tool("invalid-1", "hr_cancel_leave_request", {}),),
                ),
                PlannerTurn("参数无效", ()),
            ]
        ),
        slot_extraction=empty_slot_service(),
    )
    conversation = invalid_runtime.create_conversation(db, owner.id, "invalid events")
    invalid_runtime.run_turn(
        db,
        owner.id,
        conversation["id"],
        uuid.uuid4(),
        "撤销申请",
        request_id="lifecycle-invalid",
    )
    invalid_events = event_rows("lifecycle-invalid")
    assert [event.event_name for event in invalid_events] == [
        "hr_turn_submitted",
        "hr_intent_resolved",
        "tool_planned",
        "tool_validation_failed",
    ]
    assert invalid_events[-1].dimensions["retryable"] is False

    error_runtime = HrRuntime(
        planner=ScriptedPlanner(  # type: ignore[arg-type]
            [PlannerError("tool_provider_timeout")]
        ),
        slot_extraction=empty_slot_service(),
    )
    conversation = error_runtime.create_conversation(db, owner.id, "error events")
    error_runtime.run_turn(
        db,
        owner.id,
        conversation["id"],
        uuid.uuid4(),
        "触发模型错误",
        request_id="lifecycle-error",
    )
    error_events = event_rows("lifecycle-error")
    assert [event.event_name for event in error_events] == [
        "hr_turn_submitted",
        "hr_flow_error",
    ]
    assert error_events[-1].dimensions == {
        "error_code": "tool_provider_timeout",
        "retryable": True,
    }


def test_runtime_records_confirmation_shown_for_write_proposal(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    request_id = values["request_id"]
    assert isinstance(owner, User) and isinstance(request_id, uuid.UUID)
    runtime = HrRuntime(
        planner=ScriptedPlanner(  # type: ignore[arg-type]
            [
                PlannerTurn(
                    None,
                    (
                        planned_tool(
                            "write-1",
                            "hr_cancel_leave_request",
                            {"request_id": str(request_id)},
                        ),
                    ),
                )
            ]
        ),
        slot_extraction=empty_slot_service(),
    )
    conversation = runtime.create_conversation(db, owner.id, "write events")

    response = runtime.run_turn(
        db,
        owner.id,
        conversation["id"],
        uuid.uuid4(),
        "撤销待审批申请",
        request_id="lifecycle-write",
    )

    assert response["blocks"][-1]["type"] == "confirmation"
    events = db.scalars(
        select(ProductEvent)
        .where(ProductEvent.request_id == "lifecycle-write")
        .order_by(ProductEvent.created_at, ProductEvent.id)
    ).all()
    assert [event.event_name for event in events] == [
        "hr_turn_submitted",
        "hr_intent_resolved",
        "tool_planned",
        "confirmation_shown",
    ]


def test_runtime_exposes_deterministic_employee_and_hr_review_operations(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    other = values["other"]
    request_id = values["request_id"]
    assert isinstance(owner, User) and isinstance(other, User)
    assert isinstance(request_id, uuid.UUID)
    db.add(
        CapabilityGrant(
            user_id=other.id,
            capability=Capability.HR_LEAVE_REVIEW.value,
            scope_kind=ScopeKind.GLOBAL.value,
            is_active=True,
        )
    )
    db.flush()
    runtime = HrRuntime(planner=DirectTextPlanner())  # type: ignore[arg-type]

    assert runtime.leave_balances(db, owner.id, 2033)[0]["available"] == "9.00"
    assert runtime.list_leave_requests(db, owner.id, "pending")[0]["id"] == request_id
    assert runtime.get_leave_request(db, owner.id, request_id)["status"] == "pending"
    with pytest.raises(HrDomainError, match="leave_request_not_found"):
        runtime.get_leave_request(db, other.id, request_id)

    queue_item = runtime.review_queue(db, other.id, "pending")[0]
    detail = runtime.review_detail(db, other.id, request_id)
    assert queue_item["id"] == detail["id"] == request_id
    assert queue_item["employee_number"].startswith("R-")
    assert queue_item["employee_display_name"] == "Runtime Owner"
    assert detail["employee_number"] == queue_item["employee_number"]
    assert detail["employee_display_name"] == "Runtime Owner"
    approved = runtime.approve(
        db, other.id, request_id, uuid.uuid4(), trace_id="review/non-uuid/trace"
    )
    assert approved["status"] == "approved"
    reviewed_event = db.scalar(select(ProductEvent).where(
        ProductEvent.event_name == "leave_request_reviewed",
        ProductEvent.actor_user_id == other.id,
    ))
    assert reviewed_event is not None
    assert reviewed_event.request_id == "review/non-uuid/trace"
    assert reviewed_event.organization_unit_id == values["organization_id"]
    assert reviewed_event.dimensions["decision"] == "approved"


def test_runtime_filters_review_queue_and_detail_in_scoped_sql(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    other = values["other"]
    request_a_id = values["request_id"]
    assert isinstance(owner, User) and isinstance(other, User)
    assert isinstance(request_a_id, uuid.UUID)

    suffix = uuid.uuid4().hex
    unit_a = OrganizationUnit(code=f"RUNTIME-A-{suffix}", name="Runtime A")
    unit_b = OrganizationUnit(code=f"RUNTIME-B-{suffix}", name="Runtime B")
    reviewer = User(
        username=f"runtime-reviewer-{suffix}",
        password_hash="hash",
        role=UserRole.HR,
        is_active=True,
    )
    db.add_all([unit_a, unit_b, reviewer])
    db.flush()
    employee_a = db.scalar(
        select(EmployeeProfile).where(EmployeeProfile.user_id == owner.id)
    )
    leave_type = db.scalar(
        select(LeaveType).where(LeaveType.code == LeaveTypeCode.ANNUAL)
    )
    assert employee_a is not None and leave_type is not None
    employee_a.organization_unit_id = unit_a.id
    employee_b = EmployeeProfile(
        user_id=other.id,
        employee_number=f"R-B-{suffix}",
        display_name="Runtime B Employee",
        hire_date=date(2024, 1, 1),
        organization_unit_id=unit_b.id,
        is_active=True,
    )
    db.add(employee_b)
    db.flush()
    account_b = LeaveAccount(
        employee_id=employee_b.id,
        leave_type_id=leave_type.id,
        year=2033,
        entitled=Decimal("10.00"),
        used=Decimal("0.00"),
        reserved=Decimal("1.00"),
        version=1,
    )
    request_b = LeaveRequest(
        request_number=f"LR-RUNTIME-B-{suffix[:20]}",
        employee_id=employee_b.id,
        leave_type_id=leave_type.id,
        start_date=date(2033, 1, 6),
        end_date=date(2033, 1, 6),
        workday_count=Decimal("1.00"),
        reason="Runtime B test",
        status=LeaveRequestStatus.PENDING,
        submitted_at=datetime.now(timezone.utc),
    )
    db.add_all(
        [
            account_b,
            request_b,
            CapabilityGrant(
                user_id=reviewer.id,
                capability=Capability.HR_LEAVE_REVIEW.value,
                scope_kind=ScopeKind.UNIT_SUBTREE.value,
                organization_unit_id=unit_a.id,
                is_active=True,
            ),
        ]
    )
    db.flush()

    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", capture_statement)
    try:
        runtime = HrRuntime(
            planner=DirectTextPlanner(),  # type: ignore[arg-type]
            capability_resolver=CapabilityResolver(),
        )
        queue = runtime.review_queue(db, reviewer.id, "pending")
        detail = runtime.review_detail(db, reviewer.id, request_a_id)
        with pytest.raises(HrDomainError, match="leave_request_not_found"):
            runtime.review_detail(db, reviewer.id, request_b.id)
    finally:
        event.remove(bind, "before_cursor_execute", capture_statement)

    assert [item["id"] for item in queue] == [request_a_id]
    assert detail["id"] == request_a_id
    normalized = [" ".join(statement.lower().split()) for statement in statements]
    assert any(
        "employee_profiles.organization_unit_id in" in statement
        and "leave_requests" in statement
        for statement in normalized
    )


def test_review_event_preserves_explicit_null_target_organization(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    reviewer = values["other"]
    request_id = values["request_id"]
    assert isinstance(owner, User) and isinstance(reviewer, User)
    assert isinstance(request_id, uuid.UUID)

    owner_profile = db.scalar(
        select(EmployeeProfile).where(EmployeeProfile.user_id == owner.id)
    )
    assert owner_profile is not None
    owner_profile.organization_unit_id = None
    suffix = uuid.uuid4().hex
    reviewer_organization = OrganizationUnit(
        code=f"RUNTIME-REVIEWER-{suffix.upper()}",
        name="Reviewer Organization",
        is_active=True,
    )
    db.add(reviewer_organization)
    db.flush()
    db.add_all(
        [
            EmployeeProfile(
                user_id=reviewer.id,
                employee_number=f"R-REVIEWER-{suffix[:20]}",
                display_name="Runtime Reviewer",
                hire_date=date(2024, 1, 1),
                organization_unit_id=reviewer_organization.id,
                is_active=True,
            ),
            CapabilityGrant(
                user_id=reviewer.id,
                capability=Capability.HR_LEAVE_REVIEW.value,
                scope_kind=ScopeKind.GLOBAL.value,
                is_active=True,
            ),
        ]
    )
    db.commit()

    runtime = HrRuntime(planner=DirectTextPlanner())  # type: ignore[arg-type]
    runtime.approve(db, reviewer.id, request_id, uuid.uuid4(), trace_id="null-org")

    reviewed_event = db.scalar(
        select(ProductEvent).where(ProductEvent.request_id == "null-org")
    )
    assert reviewed_event is not None
    assert reviewed_event.organization_unit_id is None


def test_runtime_cancel_intent_is_idempotent_and_executes_only_after_confirmation(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    request_id = values["request_id"]
    assert isinstance(owner, User) and isinstance(request_id, uuid.UUID)
    runtime = HrRuntime(planner=DirectTextPlanner())  # type: ignore[arg-type]

    intent_operation = uuid.uuid4()
    intent = runtime.cancel_intent(
        db, owner.id, request_id, intent_operation,
        trace_id="cancel-intent/non-uuid/trace",
    )
    replay = runtime.cancel_intent(
        db, owner.id, request_id, intent_operation
    )
    assert intent == replay
    assert set(intent["preview"]) == {
        "request_number",
        "leave_type_code",
        "leave_type_name",
        "start_date",
        "end_date",
        "workday_count",
        "reason",
        "status",
    }
    assert runtime.get_leave_request(db, owner.id, request_id)["status"] == "pending"

    cancelled = runtime.cancel_confirmation(
        db, owner.id, intent["confirmation_id"],
        trace_id="cancel-confirmation/non-uuid/trace",
    )
    assert cancelled["status"] == "cancelled"
    assert runtime.get_leave_request(db, owner.id, request_id)["status"] == "pending"

    second = runtime.cancel_intent(
        db, owner.id, request_id, uuid.uuid4(), trace_id="cancel-intent-2"
    )
    result = runtime.confirm(
        db, owner.id, second["confirmation_id"], uuid.uuid4(),
        trace_id="cancel-confirm/non-uuid/trace",
    )
    assert result["type"] == "execution_result"
    assert result["result"]["status"] == "cancelled"
    assert runtime.get_leave_request(db, owner.id, request_id)["status"] == "cancelled"
    assert runtime.leave_balances(db, owner.id, 2033)[0]["reserved"] == "0.00"
    events = db.scalars(select(ProductEvent).where(
        ProductEvent.actor_user_id == owner.id).order_by(ProductEvent.created_at)).all()
    assert [event.event_name for event in events] == [
        "confirmation_shown",
        "confirmation_cancelled",
        "confirmation_shown",
        "confirmation_confirmed",
        "leave_request_cancelled",
    ]
    assert events[-1].organization_unit_id == values["organization_id"]
    assert events[-1].dimensions == {"leave_type": "annual"}


def test_runtime_submit_event_is_atomic_with_confirmation_and_balance(
    runtime_session: tuple[Session, dict[str, object]], monkeypatch, caplog,
) -> None:
    caplog.set_level("INFO", logger="policy_api")
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    runtime = HrRuntime(planner=DirectTextPlanner())  # type: ignore[arg-type]
    db.add_all([
        WorkCalendarDay(calendar_date=date(2033, 1, 6), kind=WorkCalendarDayKind.WORKDAY, is_workday=True),
        WorkCalendarDay(calendar_date=date(2033, 1, 7), kind=WorkCalendarDayKind.WORKDAY, is_workday=True),
    ])
    db.commit()

    def pending(start_date: str) -> ToolConfirmation:
        arguments = {
            "leave_type_code": "annual", "start_date": start_date,
            "end_date": start_date, "reason": "不得进入产品事件",
        }
        invocation = ToolInvocation(
            conversation_id=uuid.uuid4(), turn_id=uuid.uuid4(),
            actor_user_id=owner.id, provider_call_id=f"submit-{uuid.uuid4()}",
            tool_name="hr.submit_leave_request",
            provider_tool_name="hr_submit_leave_request", risk_level="write",
            status=ToolInvocationStatus.PROPOSED,
            arguments_hash=canonical_arguments_hash(arguments),
        )
        db.add(invocation); db.flush()
        confirmation = create_tool_confirmation(
            db, invocation=invocation, owner_user_id=owner.id,
            normalized_arguments=arguments, preview={},
            expires_at=datetime.now(timezone.utc).replace(year=2034),
            audit_summary=AuditSummary.for_execution(
                tool_name=invocation.tool_name, risk_level="write", outcome="pending"),
        )
        db.commit()
        return confirmation

    confirmation = pending("2033-01-06")
    result = runtime.confirm(
        db, owner.id, confirmation.id, uuid.uuid4(), trace_id="submit/trace"
    )
    assert result["result"]["status"] == "pending"
    events = db.scalars(select(ProductEvent).where(
        ProductEvent.actor_user_id == owner.id).order_by(ProductEvent.created_at)).all()
    assert [event.event_name for event in events] == [
        "confirmation_confirmed", "leave_request_submitted",
    ]
    assert events[-1].organization_unit_id == values["organization_id"]
    assert events[-1].dimensions == {
        "leave_type": "annual", "workday_count_bucket": "0_1",
    }
    audit_kinds = db.scalars(
        select(ToolAuditEvent.event_kind)
        .where(ToolAuditEvent.confirmation_id == confirmation.id)
        .order_by(ToolAuditEvent.created_at, ToolAuditEvent.id)
    ).all()
    assert audit_kinds == [
        ToolAuditEventKind.CONFIRMATION_CREATED,
        ToolAuditEventKind.EXECUTION_STARTED,
        ToolAuditEventKind.EXECUTION_SUCCEEDED,
    ]

    account = db.scalar(select(LeaveAccount).where(LeaveAccount.employee_id ==
        select(EmployeeProfile.id).where(EmployeeProfile.user_id == owner.id).scalar_subquery()))
    assert account is not None
    reserved_before = account.reserved
    requests_before = db.scalar(select(func.count()).select_from(LeaveRequest))
    failing = pending("2033-01-07")
    original_append = runtime.product_event_emitter.append

    def reject_last_event(session, event):
        if event.event_name == "leave_request_submitted":
            raise ProductEventValidationError("event_dimensions_invalid")
        return original_append(session, event)

    monkeypatch.setattr(runtime.product_event_emitter, "append", reject_last_event)
    with pytest.raises(ToolError, match="tool_execution_non_retryable"):
        runtime.confirm(db, owner.id, failing.id, uuid.uuid4(), trace_id="submit/fail")
    db.refresh(failing); db.refresh(account)
    assert failing.status.value == "pending"
    assert account.reserved == reserved_before
    assert db.scalar(select(func.count()).select_from(LeaveRequest)) == requests_before
    assert db.scalars(select(ProductEvent).where(
        ProductEvent.request_id == "submit/fail")).all() == []
    failing_invocation = db.get(ToolInvocation, failing.invocation_id)
    assert failing_invocation is not None
    assert failing_invocation.status == ToolInvocationStatus.FAILED
    assert failing_invocation.error_code == "tool_execution_non_retryable"
    assert '"stage":"product_event"' in caplog.text
    assert '"error_code":"event_dimensions_invalid"' in caplog.text
    assert "不得进入产品事件" not in caplog.text
    failing_audit_kinds = db.scalars(
        select(ToolAuditEvent.event_kind)
        .where(ToolAuditEvent.confirmation_id == failing.id)
        .order_by(ToolAuditEvent.created_at, ToolAuditEvent.id)
    ).all()
    assert failing_audit_kinds == [
        ToolAuditEventKind.CONFIRMATION_CREATED,
        ToolAuditEventKind.EXECUTION_FAILED,
    ]


def test_runtime_compensatory_submit_and_cancel_is_atomic_with_product_events(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    employee = db.scalar(
        select(EmployeeProfile).where(EmployeeProfile.user_id == owner.id)
    )
    assert employee is not None
    leave_type = LeaveType(
        code=LeaveTypeCode.COMPENSATORY,
        display_name="Compensatory leave",
        is_enabled=True,
    )
    db.add(leave_type)
    db.flush()
    account = LeaveAccount(
        employee_id=employee.id,
        leave_type_id=leave_type.id,
        year=2033,
        entitled=Decimal("4.00"),
        used=Decimal("0.00"),
        reserved=Decimal("0.00"),
        version=1,
    )
    db.add_all(
        [
            account,
            WorkCalendarDay(
                calendar_date=date(2033, 1, 8),
                kind=WorkCalendarDayKind.WORKDAY,
                is_workday=True,
            ),
            WorkCalendarDay(
                calendar_date=date(2033, 1, 9),
                kind=WorkCalendarDayKind.WORKDAY,
                is_workday=True,
            ),
        ]
    )
    arguments = {
        "leave_type_code": LeaveTypeCode.COMPENSATORY.value,
        "start_date": "2033-01-08",
        "end_date": "2033-01-09",
        "reason": "家庭事务",
    }
    invocation = ToolInvocation(
        conversation_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        actor_user_id=owner.id,
        provider_call_id=f"compensatory-{uuid.uuid4()}",
        tool_name="hr.submit_leave_request",
        provider_tool_name="hr_submit_leave_request",
        risk_level="write",
        status=ToolInvocationStatus.PROPOSED,
        arguments_hash=canonical_arguments_hash(arguments),
    )
    db.add(invocation)
    db.flush()
    confirmation = create_tool_confirmation(
        db,
        invocation=invocation,
        owner_user_id=owner.id,
        normalized_arguments=arguments,
        preview={},
        expires_at=datetime.now(timezone.utc).replace(year=2034),
        audit_summary=AuditSummary.for_execution(
            tool_name=invocation.tool_name,
            risk_level="write",
            outcome="pending",
        ),
    )
    db.commit()
    runtime = HrRuntime(planner=DirectTextPlanner())  # type: ignore[arg-type]

    submitted = runtime.confirm(
        db,
        owner.id,
        confirmation.id,
        uuid.uuid4(),
        trace_id="compensatory/submit",
    )
    request_id = uuid.UUID(str(submitted["resource_id"]))
    db.refresh(account)
    request = db.get(LeaveRequest, request_id)
    assert request is not None
    assert request.status == LeaveRequestStatus.PENDING
    assert request.leave_type_id == leave_type.id
    assert account.reserved == Decimal("2.00")
    submitted_event = db.scalar(
        select(ProductEvent).where(
            ProductEvent.actor_user_id == owner.id,
            ProductEvent.event_name == "leave_request_submitted",
        )
    )
    assert submitted_event is not None
    assert submitted_event.dimensions == {
        "leave_type": LeaveTypeCode.COMPENSATORY.value,
        "workday_count_bucket": "1_2",
    }

    cancel_intent = runtime.cancel_intent(
        db,
        owner.id,
        request_id,
        uuid.uuid4(),
        trace_id="compensatory/cancel-intent",
    )
    cancelled = runtime.confirm(
        db,
        owner.id,
        cancel_intent["confirmation_id"],
        uuid.uuid4(),
        trace_id="compensatory/cancel",
    )
    db.refresh(account)
    db.refresh(request)
    assert cancelled["result"]["status"] == LeaveRequestStatus.CANCELLED.value
    assert request.status == LeaveRequestStatus.CANCELLED
    assert account.reserved == Decimal("0.00")
    cancelled_event = db.scalar(
        select(ProductEvent).where(
            ProductEvent.actor_user_id == owner.id,
            ProductEvent.event_name == "leave_request_cancelled",
        )
    )
    assert cancelled_event is not None
    assert cancelled_event.dimensions == {
        "leave_type": LeaveTypeCode.COMPENSATORY.value,
    }


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_review_event_failure_rolls_back_request_balance_and_business_event(
    runtime_session: tuple[Session, dict[str, object]], monkeypatch, decision: str,
) -> None:
    db, values = runtime_session
    reviewer = values["other"]
    request_id = values["request_id"]
    assert isinstance(reviewer, User) and isinstance(request_id, uuid.UUID)
    db.add(
        CapabilityGrant(
            user_id=reviewer.id,
            capability=Capability.HR_LEAVE_REVIEW.value,
            scope_kind=ScopeKind.GLOBAL.value,
            is_active=True,
        )
    )
    db.commit()
    runtime = HrRuntime(planner=DirectTextPlanner())  # type: ignore[arg-type]
    original_append = runtime.product_event_emitter.append

    def reject_review_event(session, event):  # type: ignore[no-untyped-def]
        if event.event_name == "leave_request_reviewed":
            raise ProductEventValidationError("event_dimensions_invalid")
        return original_append(session, event)

    monkeypatch.setattr(
        runtime.product_event_emitter, "append", reject_review_event
    )
    operation_id = uuid.uuid4()
    with pytest.raises(
        ProductEventValidationError, match="event_dimensions_invalid"
    ):
        if decision == "approve":
            runtime.approve(
                db,
                reviewer.id,
                request_id,
                operation_id,
                trace_id=f"{decision}-atomic-fail",
            )
        else:
            runtime.reject(
                db,
                reviewer.id,
                request_id,
                operation_id,
                "排班冲突",
                trace_id=f"{decision}-atomic-fail",
            )

    db.expire_all()
    request = db.get(LeaveRequest, request_id)
    assert request is not None
    assert request.status == LeaveRequestStatus.PENDING
    account = db.get(LeaveAccount, request.employee_id)
    if account is None:
        account = db.scalar(
            select(LeaveAccount).where(
                LeaveAccount.employee_id == request.employee_id
            )
        )
    assert account is not None
    assert account.used == Decimal("0.00")
    assert account.reserved == Decimal("1.00")
    assert db.scalar(
        select(func.count())
        .select_from(LeaveAccountEvent)
        .where(LeaveAccountEvent.operation_id == operation_id)
    ) == 0
    assert db.scalars(
        select(ProductEvent).where(
            ProductEvent.request_id == f"{decision}-atomic-fail"
        )
    ).all() == []


def test_cancel_event_failure_rolls_back_confirmation_request_and_balance(
    runtime_session: tuple[Session, dict[str, object]], monkeypatch,
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    request_id = values["request_id"]
    assert isinstance(owner, User) and isinstance(request_id, uuid.UUID)
    runtime = HrRuntime(planner=DirectTextPlanner())  # type: ignore[arg-type]
    intent = runtime.cancel_intent(
        db, owner.id, request_id, uuid.uuid4(), trace_id="cancel-seed"
    )
    confirmation = db.get(ToolConfirmation, intent["confirmation_id"])
    assert confirmation is not None
    original_append = runtime.product_event_emitter.append

    def reject_cancel_event(session, event):  # type: ignore[no-untyped-def]
        if event.event_name == "leave_request_cancelled":
            raise ProductEventValidationError("event_dimensions_invalid")
        return original_append(session, event)

    monkeypatch.setattr(
        runtime.product_event_emitter, "append", reject_cancel_event
    )
    operation_id = uuid.uuid4()
    with pytest.raises(ToolError, match="tool_execution_non_retryable"):
        runtime.confirm(
            db,
            owner.id,
            confirmation.id,
            operation_id,
            trace_id="cancel-atomic-fail",
        )

    db.refresh(confirmation)
    request = db.get(LeaveRequest, request_id)
    assert request is not None
    account = db.scalar(
        select(LeaveAccount).where(
            LeaveAccount.employee_id == request.employee_id
        )
    )
    assert confirmation.status.value == "pending"
    assert request.status == LeaveRequestStatus.PENDING
    assert account is not None and account.reserved == Decimal("1.00")
    assert db.scalar(
        select(func.count())
        .select_from(LeaveAccountEvent)
        .where(LeaveAccountEvent.operation_id == operation_id)
    ) == 0
    assert db.scalars(
        select(ProductEvent).where(
            ProductEvent.request_id == "cancel-atomic-fail"
        )
    ).all() == []


def test_confirmation_expiry_event_and_callback_are_atomic(
    runtime_session: tuple[Session, dict[str, object]], monkeypatch,
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    request_id = values["request_id"]
    assert isinstance(owner, User) and isinstance(request_id, uuid.UUID)
    runtime = HrRuntime(planner=DirectTextPlanner())  # type: ignore[arg-type]

    expired_intent = runtime.cancel_intent(
        db, owner.id, request_id, uuid.uuid4(), trace_id="expiry-seed"
    )
    expired = db.get(ToolConfirmation, expired_intent["confirmation_id"])
    assert expired is not None
    expired.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    with pytest.raises(ToolError, match="confirmation_expired"):
        runtime.confirm(
            db,
            owner.id,
            expired.id,
            uuid.uuid4(),
            trace_id="expiry-success",
        )
    db.refresh(expired)
    assert expired.status.value == "expired"
    expiry_events = db.scalars(
        select(ProductEvent).where(ProductEvent.request_id == "expiry-success")
    ).all()
    assert [event.event_name for event in expiry_events] == [
        "confirmation_expired"
    ]

    failing_intent = runtime.cancel_intent(
        db, owner.id, request_id, uuid.uuid4(), trace_id="expiry-fail-seed"
    )
    failing = db.get(ToolConfirmation, failing_intent["confirmation_id"])
    assert failing is not None
    failing.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    original_append = runtime.product_event_emitter.append

    def reject_expiry_event(session, event):  # type: ignore[no-untyped-def]
        if event.event_name == "confirmation_expired":
            raise ProductEventValidationError("event_dimensions_invalid")
        return original_append(session, event)

    monkeypatch.setattr(
        runtime.product_event_emitter, "append", reject_expiry_event
    )
    with pytest.raises(ToolError, match="tool_execution_non_retryable"):
        runtime.confirm(
            db,
            owner.id,
            failing.id,
            uuid.uuid4(),
            trace_id="expiry-atomic-fail",
        )
    db.refresh(failing)
    assert failing.status.value == "pending"
    assert db.scalars(
        select(ProductEvent).where(
            ProductEvent.request_id == "expiry-atomic-fail"
        )
    ).all() == []


def test_cancel_endpoint_persists_first_expiry_with_product_event(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    request_id = values["request_id"]
    assert isinstance(owner, User) and isinstance(request_id, uuid.UUID)
    runtime = HrRuntime(planner=DirectTextPlanner())  # type: ignore[arg-type]
    intent = runtime.cancel_intent(
        db, owner.id, request_id, uuid.uuid4(), trace_id="cancel-expiry-seed"
    )
    confirmation = db.get(ToolConfirmation, intent["confirmation_id"])
    assert confirmation is not None
    confirmation.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    with pytest.raises(ToolError, match="confirmation_expired"):
        runtime.cancel_confirmation(
            db,
            owner.id,
            confirmation.id,
            trace_id="cancel-expiry-transition",
        )

    db.rollback()
    db.refresh(confirmation)
    assert confirmation.status.value == "expired"
    events = db.scalars(
        select(ProductEvent).where(
            ProductEvent.request_id == "cancel-expiry-transition"
        )
    ).all()
    assert [event.event_name for event in events] == [
        "confirmation_expired"
    ]

    with pytest.raises(ToolError, match="confirmation_expired"):
        runtime.cancel_confirmation(
            db,
            owner.id,
            confirmation.id,
            trace_id="cancel-expiry-replay",
        )
    assert db.scalars(
        select(ProductEvent).where(
            ProductEvent.request_id == "cancel-expiry-replay"
        )
    ).all() == []
