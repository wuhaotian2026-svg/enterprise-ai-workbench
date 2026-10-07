from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from policy_api.approvals.models import ApprovalInstance, ApprovalTask
from policy_api.assistant_drafts.models import AssistantFlowDraft, DraftStatus
from policy_api.assistant_drafts.store import AssistantDraftStore
from policy_api.hr.runtime import HrRuntime
from policy_api.hr.schemas import HrDomainError
from policy_api.hr.enums import LeaveTypeCode, WorkCalendarDayKind
from policy_api.hr.models import (
    EmployeeProfile,
    LeaveAccount,
    LeaveRequest,
    LeaveType,
    WorkCalendarDay,
)
from policy_api.hr.slot_schema import HR_SLOT_SCHEMA
from policy_api.models import User
from policy_api.procurement.models import AssistantTurn, ProcurementRequest
from policy_api.procurement.observability import ProcurementObservability
from policy_api.procurement.repository import ProcurementRepository
from policy_api.procurement.runtime import ProcurementRuntime
from policy_api.procurement.slot_schema import PROCUREMENT_SLOT_SCHEMA
from policy_api.procurement.tool_flow_policy import ProcurementToolFlowPolicy
from policy_api.slot_extraction.errors import SlotExtractionError
from policy_api.slot_extraction.fingerprint import SlotExtractionFingerprinter
from policy_api.slot_extraction.models import SlotExtractionOperationStatus
from policy_api.slot_extraction.repository import SlotExtractionOperationRepository
from policy_api.slot_extraction.schemas import SlotCandidate, SlotExtractionEnvelope
from policy_api.slot_extraction.service import (
    SlotExtractionPreparationRequest,
    SlotExtractionService,
)
from policy_api.tools.models import ToolConfirmation
from policy_api.tools.errors import ToolError
from policy_api.tools.types import PlannedToolCall, PlannerTurn
from policy_api.workbench.capabilities import CapabilityResolver
from tests.integration.test_hr_runtime import runtime_session
from tests.integration.test_procurement_submission_transactions import (
    ProcurementFixture,
    procurement_fixture,
)


class SequentialPlanner:
    model = "test-assistant-draft-planner"

    def __init__(self, turns: Sequence[PlannerTurn]) -> None:
        self._turns = list(turns)
        self.tools_seen: list[list[str]] = []
        self.required_tool_names_seen: list[str | None] = []

    def complete(  # type: ignore[no-untyped-def]
        self, _messages, *, tools, required_tool_name=None
    ):
        self.tools_seen.append([
            str(tool["function"]["name"])
            for tool in tools
        ])
        self.required_tool_names_seen.append(required_tool_name)
        return self._turns.pop(0)


class RecordingSlotClient:
    model = "test-slot-extractor"

    def __init__(self, outcomes: Sequence[SlotExtractionEnvelope | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.inputs: list[tuple[str, object]] = []

    def extract(self, *, current_user_turn_text, slot_schema):  # type: ignore[no-untyped-def]
        self.inputs.append((current_user_turn_text, slot_schema.provider_json))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ExplodingPlanner:
    model = "planner-must-not-run"

    def complete(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("planner must not run")


def _hr_slot_service(
    outcomes: Sequence[SlotExtractionEnvelope | Exception],
    *,
    now_provider: Callable[[], datetime] | None = None,
) -> tuple[SlotExtractionService, RecordingSlotClient]:
    client = RecordingSlotClient(outcomes)
    return (
        SlotExtractionService(
            client=client,
            repository=SlotExtractionOperationRepository(),
            fingerprinter=SlotExtractionFingerprinter(b"test-slot-secret" * 3),
            model_timeout_seconds=30,
            now_provider=now_provider,
        ),
        client,
    )


def _procurement_slot_service(
    outcomes: Sequence[SlotExtractionEnvelope | Exception],
    *,
    now_provider: Callable[[], datetime] | None = None,
) -> tuple[SlotExtractionService, RecordingSlotClient]:
    client = RecordingSlotClient(outcomes)
    return (
        SlotExtractionService(
            client=client,
            repository=SlotExtractionOperationRepository(),
            fingerprinter=SlotExtractionFingerprinter(b"test-slot-secret" * 3),
            model_timeout_seconds=30,
            now_provider=now_provider,
        ),
        client,
    )


def _draft_block(result: dict[str, object]) -> dict[str, object]:
    blocks = result["blocks"]
    assert isinstance(blocks, (list, tuple))
    return next(
        block
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "assistant_draft"
    )


def test_hr_extractor_receives_only_current_turn_and_reuses_validated_draft(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    slot_service, extractor = _hr_slot_service(
        [
            SlotExtractionEnvelope(
                schema_version=HR_SLOT_SCHEMA.version,
                candidates=[
                    SlotCandidate(
                        slot_name="leave_type_code",
                        raw_value="年假",
                        source_quote="年假",
                    ),
                    SlotCandidate(
                        slot_name="date_range",
                        raw_value="2033-01-06到2033-01-06",
                        source_quote="2033-01-06到2033-01-06",
                    ),
                ],
            ),
            SlotExtractionEnvelope(
                schema_version=HR_SLOT_SCHEMA.version,
                candidates=[SlotCandidate(
                    slot_name="reason",
                    raw_value="探亲",
                    source_quote="用于探亲",
                )],
            ),
        ]
    )
    planner = SequentialPlanner(
        [
            PlannerTurn(
                text=None,
                tool_calls=(PlannedToolCall(
                    call_id="balance-current-turn",
                    name="hr_get_my_leave_balances",
                    arguments={},
                ),),
            ),
            PlannerTurn(
                text=None,
                tool_calls=(PlannedToolCall(
                    call_id="duration-current-turn",
                    name="hr_calculate_leave_duration",
                    arguments={},
                ),),
            ),
            PlannerTurn(
                text=None,
                tool_calls=(PlannedToolCall(
                    call_id="submit-current-turn",
                    name="hr_submit_leave_request",
                    arguments={},
                ),),
            ),
        ]
    )
    runtime = HrRuntime(
        planner=planner,  # type: ignore[arg-type]
        slot_extraction=slot_service,
    )
    db.add(WorkCalendarDay(
        calendar_date=date(2033, 1, 6),
        kind=WorkCalendarDayKind.WORKDAY,
        is_workday=True,
    ))
    db.commit()
    conversation_id = runtime.create_conversation(
        db, owner.id, "current-turn-only extraction"
    )["id"]

    first_text = "我想申请2033-01-06到2033-01-06年假"
    first = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        first_text,
    )
    assert first["slot_extraction_calls"] == 1
    assert first["model_calls"] == 0
    assert planner.tools_seen == []

    second_text = "原因用于探亲"
    second = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        second_text,
    )

    assert extractor.inputs == [
        (first_text, HR_SLOT_SCHEMA.provider_json),
        (second_text, HR_SLOT_SCHEMA.provider_json),
    ]
    assert first_text not in repr(extractor.inputs[1][1])
    assert second["slot_extraction_calls"] == 1
    confirmation = next(
        block for block in second["blocks"] if block["type"] == "confirmation"
    )
    assert {
        key: confirmation["preview"][key]
        for key in ("leave_type_code", "start_date", "end_date", "reason")
    } == {
        "leave_type_code": "annual",
        "start_date": "2033-01-06",
        "end_date": "2033-01-06",
        "reason": "探亲",
    }


def test_hr_extraction_failure_preserves_draft_and_never_calls_planner(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    service, extractor = _hr_slot_service(
        [
            SlotExtractionEnvelope(
                schema_version=HR_SLOT_SCHEMA.version,
                candidates=[SlotCandidate(
                    slot_name="leave_type_code",
                    raw_value="年假",
                    source_quote="年假",
                )],
            ),
            SlotExtractionError("slot_extraction_timeout", retryable=True),
        ]
    )
    runtime = HrRuntime(
        planner=ExplodingPlanner(),  # type: ignore[arg-type]
        slot_extraction=service,
    )
    conversation_id = runtime.create_conversation(
        db, owner.id, "fail closed extraction"
    )["id"]
    runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        "我要请年假",
    )
    before = db.scalar(select(AssistantFlowDraft).where(
        AssistantFlowDraft.owner_user_id == owner.id,
        AssistantFlowDraft.module_key == "hr",
        AssistantFlowDraft.conversation_id == conversation_id,
    ))
    assert before is not None
    before_state = (
        dict(before.field_values),
        dict(before.pending_candidates),
        before.version,
    )
    failed_turn_id = uuid.uuid4()

    failed = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        failed_turn_id,
        "日期是2033-01-06",
    )
    replay = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        failed_turn_id,
        "日期是2033-01-06",
    )
    db.refresh(before)

    assert failed == replay
    assert len(extractor.inputs) == 2
    assert failed["slot_extraction_calls"] == 1
    assert failed["model_calls"] == 0
    assert failed["read_calls"] == 0
    assert failed["write_proposals"] == 0
    assert any(
        block["type"] == "error" and block["code"] == "slot_extraction_timeout"
        for block in failed["blocks"]
    )
    assert (
        dict(before.field_values),
        dict(before.pending_candidates),
        before.version,
    ) == before_state

    with pytest.raises(HrDomainError, match="client_turn_id_conflict"):
        runtime.run_turn(
            db,
            owner.id,
            conversation_id,  # type: ignore[arg-type]
            failed_turn_id,
            "另一段文本",
        )
    assert len(extractor.inputs) == 2


@pytest.mark.parametrize("initial_status", ["reserved", "dispatched"])
def test_hr_stale_extraction_converges_without_redispatch_or_draft_mutation(
    runtime_session: tuple[Session, dict[str, object]],
    initial_status: str,
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    old_now = datetime.now(timezone.utc) - timedelta(minutes=5)
    service, extractor = _hr_slot_service([])
    runtime = HrRuntime(
        planner=ExplodingPlanner(),  # type: ignore[arg-type]
        slot_extraction=service,
    )
    conversation_id = runtime.create_conversation(
        db, owner.id, f"stale {initial_status}"
    )["id"]
    client_turn_id = uuid.uuid4()
    text = "我要请年假"
    request = SlotExtractionPreparationRequest(
        owner_user_id=owner.id,
        module_key="hr",
        conversation_id=conversation_id,  # type: ignore[arg-type]
        client_turn_id=client_turn_id,
        current_user_turn_text=text,
        slot_schema=HR_SLOT_SCHEMA,
    )
    fingerprint = service.fingerprinter.fingerprint(request.identity())
    claim = service.repository.claim(
        db,
        request.identity(),
        fingerprint,
        model_name=extractor.model,
        now=old_now,
        timeout_seconds=1,
    )
    if initial_status == "dispatched":
        service.repository.mark_dispatched(
            db,
            claim.operation.id,
            now=old_now,
            timeout_seconds=1,
        )
    db.commit()

    response = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        client_turn_id,
        text,
    )
    db.refresh(claim.operation)

    assert extractor.inputs == []
    assert response["slot_extraction_calls"] == 0
    assert response["model_calls"] == 0
    assert any(
        block["type"] == "error"
        and block["code"] == "slot_extraction_outcome_indeterminate"
        for block in response["blocks"]
    )
    assert claim.operation.status == SlotExtractionOperationStatus.INDETERMINATE
    assert db.scalar(select(AssistantFlowDraft).where(
        AssistantFlowDraft.owner_user_id == owner.id,
        AssistantFlowDraft.module_key == "hr",
        AssistantFlowDraft.conversation_id == conversation_id,
    )) is None


def test_hr_late_extraction_result_cannot_merge_or_call_planner(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    started_at = datetime.now(timezone.utc)
    times = iter((started_at, started_at, started_at + timedelta(seconds=31)))
    service, extractor = _hr_slot_service(
        [SlotExtractionEnvelope(
            schema_version=HR_SLOT_SCHEMA.version,
            candidates=[
                SlotCandidate(
                    slot_name="leave_type_code",
                    raw_value="年假",
                    source_quote="年假",
                ),
                SlotCandidate(
                    slot_name="start_date",
                    raw_value="2033-01-06",
                    source_quote="2033-01-06",
                ),
                SlotCandidate(
                    slot_name="end_date",
                    raw_value="2033-01-06",
                    source_quote="2033-01-06",
                ),
                SlotCandidate(
                    slot_name="reason",
                    raw_value="探亲",
                    source_quote="用于探亲",
                ),
            ],
        )],
        now_provider=lambda: next(times),
    )
    runtime = HrRuntime(
        planner=ExplodingPlanner(),  # type: ignore[arg-type]
        slot_extraction=service,
    )
    conversation_id = runtime.create_conversation(
        db, owner.id, "late extraction"
    )["id"]

    response = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        "我要请2033-01-06一天年假，用于探亲",
    )

    assert len(extractor.inputs) == 1
    assert response["model_calls"] == 0
    assert response["write_proposals"] == 0
    assert any(
        block["type"] == "error"
        and block["code"] == "slot_extraction_outcome_indeterminate"
        for block in response["blocks"]
    )
    assert db.scalar(select(AssistantFlowDraft).where(
        AssistantFlowDraft.owner_user_id == owner.id,
        AssistantFlowDraft.module_key == "hr",
        AssistantFlowDraft.conversation_id == conversation_id,
    )) is None


def test_hr_draft_version_conflict_blocks_confirmation(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)

    class ConflictingDraftStore(AssistantDraftStore):
        def __init__(self) -> None:
            super().__init__()
            self.assertions = 0

        def assert_current_version(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.assertions += 1
            raise ToolError("assistant_draft_version_conflict")

    service, _extractor = _hr_slot_service([SlotExtractionEnvelope(
        schema_version=HR_SLOT_SCHEMA.version,
        candidates=[
            SlotCandidate(
                slot_name="leave_type_code",
                raw_value="年假",
                source_quote="年假",
            ),
            SlotCandidate(
                slot_name="start_date",
                raw_value="2033-01-06",
                source_quote="2033-01-06",
            ),
            SlotCandidate(
                slot_name="end_date",
                raw_value="2033-01-06",
                source_quote="2033-01-06",
            ),
            SlotCandidate(
                slot_name="reason",
                raw_value="探亲",
                source_quote="用于探亲",
            ),
        ],
    )])
    planner = SequentialPlanner([
        PlannerTurn(None, (PlannedToolCall(
            call_id="balance-version", name="hr_get_my_leave_balances", arguments={},
        ),)),
        PlannerTurn(None, (PlannedToolCall(
            call_id="duration-version", name="hr_calculate_leave_duration", arguments={},
        ),)),
        PlannerTurn(None, (PlannedToolCall(
            call_id="submit-version", name="hr_submit_leave_request", arguments={},
        ),)),
    ])
    draft_store = ConflictingDraftStore()
    runtime = HrRuntime(
        planner=planner,  # type: ignore[arg-type]
        slot_extraction=service,
        draft_store=draft_store,
    )
    db.add(WorkCalendarDay(
        calendar_date=date(2033, 1, 6),
        kind=WorkCalendarDayKind.WORKDAY,
        is_workday=True,
    ))
    db.commit()
    conversation_id = runtime.create_conversation(
        db, owner.id, "version conflict"
    )["id"]
    before = db.scalar(select(func.count(ToolConfirmation.id)))

    response = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        "我要请2033-01-06一天年假，用于探亲",
    )

    assert draft_store.assertions == 1
    assert db.scalar(select(func.count(ToolConfirmation.id))) == before
    assert response["write_proposals"] == 0
    assert any(
        block["type"] == "error"
        and block["code"] == "assistant_draft_version_conflict"
        for block in response["blocks"]
    )


def test_hr_follow_up_only_supplies_missing_year_and_proposes_from_scoped_draft(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    slot_service, extractor = _hr_slot_service([
        SlotExtractionEnvelope(
            schema_version=HR_SLOT_SCHEMA.version,
            candidates=[
                SlotCandidate(
                    slot_name="date_range",
                    raw_value="1.6-1.6",
                    source_quote="1.6-1.6",
                ),
                SlotCandidate(
                    slot_name="leave_type_code",
                    raw_value="年假",
                    source_quote="年假",
                ),
                SlotCandidate(
                    slot_name="reason",
                    raw_value="就医",
                    source_quote="原因是就医",
                ),
            ],
        ),
        SlotExtractionEnvelope(
            schema_version=HR_SLOT_SCHEMA.version,
            candidates=[SlotCandidate(
                slot_name="year",
                raw_value="就是2033年",
                source_quote="就是2033年",
            )],
        ),
    ])
    planner = SequentialPlanner([
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="balance-from-draft",
                name="hr_get_my_leave_balances",
                arguments={},
            ),),
        ),
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="duration-from-draft",
                name="hr_calculate_leave_duration",
                arguments={},
            ),),
        ),
        PlannerTurn(
            text=None,
            tool_calls=(
                PlannedToolCall(
                    call_id="submit-from-draft",
                    name="hr_submit_leave_request",
                    arguments={},
                ),
            ),
        ),
    ])
    runtime = HrRuntime(
        planner=planner,  # type: ignore[arg-type]
        slot_extraction=slot_service,
    )
    db.add(WorkCalendarDay(
        calendar_date=date(2033, 1, 6),
        kind=WorkCalendarDayKind.WORKDAY,
        is_workday=True,
    ))
    db.commit()
    conversation_id = runtime.create_conversation(
        db, owner.id, "增量补充请假信息"
    )["id"]

    first = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        "1.6-1.6，我要请年假，原因是就医",
    )
    first_draft = _draft_block(first)
    assert first_draft["fields"] == {
        "leave_type_code": "annual",
        "reason": "就医",
    }
    assert first_draft["pending_fields"] == []
    assert first_draft["missing_fields"] == ["year"]
    first_text = next(
        block["text"] for block in first["blocks"] if block["type"] == "text"
    )
    assert first_text == (
        "已记录本会话中你明确提供的请假信息，请只补充：请假年份。"
        "无需重复已经提供的内容。"
    )
    assert "开始日期" not in first_text
    assert "结束日期" not in first_text

    before_confirmations = db.scalar(select(func.count(ToolConfirmation.id)))
    second = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        "就是2033年",
    )

    assert db.scalar(select(func.count(ToolConfirmation.id))) == (
        before_confirmations + 1
    )
    confirmation = next(
        block for block in second["blocks"] if block["type"] == "confirmation"
    )
    assert confirmation["preview"]["leave_type_code"] == "annual"
    assert confirmation["preview"]["start_date"] == "2033-01-06"
    assert confirmation["preview"]["end_date"] == "2033-01-06"
    assert confirmation["preview"]["reason"] == "就医"
    assert extractor.inputs == [
        ("1.6-1.6，我要请年假，原因是就医", HR_SLOT_SCHEMA.provider_json),
        ("就是2033年", HR_SLOT_SCHEMA.provider_json),
    ]
    assert planner.required_tool_names_seen == [
        "hr_get_my_leave_balances",
        "hr_calculate_leave_duration",
        "hr_submit_leave_request",
    ]
    draft = db.scalar(select(AssistantFlowDraft).where(
        AssistantFlowDraft.owner_user_id == owner.id,
        AssistantFlowDraft.module_key == "hr",
        AssistantFlowDraft.conversation_id == conversation_id,
    ))
    assert draft is not None
    assert draft.status == DraftStatus.CLOSED.value


def test_hr_unknown_first_turn_activates_and_preserves_exact_multiturn_draft(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    employee = db.scalar(select(EmployeeProfile).where(
        EmployeeProfile.user_id == owner.id,
    ))
    assert employee is not None
    compensatory = LeaveType(
        code=LeaveTypeCode.COMPENSATORY,
        display_name="Compensatory leave",
        is_enabled=True,
    )
    db.add(compensatory)
    db.flush()
    db.add(LeaveAccount(
        employee_id=employee.id,
        leave_type_id=compensatory.id,
        year=2033,
        entitled=Decimal("10.00"),
        used=Decimal("0.00"),
        reserved=Decimal("0.00"),
        version=1,
    ))
    db.add_all([
        WorkCalendarDay(
            calendar_date=calendar_date,
            kind=WorkCalendarDayKind.WORKDAY,
            is_workday=True,
        )
        for calendar_date in (
            date(2033, 9, 20),
            date(2033, 9, 21),
            date(2033, 9, 22),
        )
    ])
    db.commit()

    slot_service, extractor = _hr_slot_service([
        SlotExtractionEnvelope(
            schema_version=HR_SLOT_SCHEMA.version,
            candidates=[
                SlotCandidate(
                    slot_name="date_range",
                    raw_value="9.20-9.22",
                    source_quote="9.20-9.22",
                ),
                SlotCandidate(
                    slot_name="reason",
                    raw_value="看病",
                    source_quote="用于看病",
                ),
            ],
        ),
        SlotExtractionEnvelope(
            schema_version=HR_SLOT_SCHEMA.version,
            candidates=[
                SlotCandidate(
                    slot_name="leave_type_code",
                    raw_value="调休",
                    source_quote="调休",
                ),
                SlotCandidate(
                    slot_name="reason",
                    raw_value="看病",
                    source_quote="用于看病",
                ),
            ],
        ),
        SlotExtractionEnvelope(
            schema_version=HR_SLOT_SCHEMA.version,
            candidates=[SlotCandidate(
                slot_name="date_range",
                raw_value="9.20-9.22",
                source_quote="9.20-9.22",
            )],
        ),
        SlotExtractionEnvelope(
            schema_version=HR_SLOT_SCHEMA.version,
            candidates=[SlotCandidate(
                slot_name="year",
                raw_value="2033年",
                source_quote="2033年",
            )],
        ),
    ])
    planner = SequentialPlanner([
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="balance-after-unknown-activation",
                name="hr_get_my_leave_balances",
                arguments={},
            ),),
        ),
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="duration-after-unknown-activation",
                name="hr_calculate_leave_duration",
                arguments={},
            ),),
        ),
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="submit-after-unknown-activation",
                name="hr_submit_leave_request",
                arguments={},
            ),),
        ),
    ])
    runtime = HrRuntime(
        planner=planner,  # type: ignore[arg-type]
        slot_extraction=slot_service,
    )
    conversation_id = runtime.create_conversation(
        db, owner.id, "unknown-intent draft activation"
    )["id"]
    before_requests = db.scalar(select(func.count(LeaveRequest.id)))
    before_confirmations = db.scalar(select(func.count(ToolConfirmation.id)))
    versions: list[int] = []

    turns = [
        "我想在9.20-9.22请假 用于看病",
        "调休 用于看病",
        "9.20-9.22",
    ]
    expected_missing = [
        ["leave_type_code", "year"],
        ["year"],
        ["year"],
    ]
    for index, turn_text in enumerate(turns):
        response = runtime.run_turn(
            db,
            owner.id,
            conversation_id,  # type: ignore[arg-type]
            uuid.uuid4(),
            turn_text,
        )
        assert response["slot_extraction_calls"] == 1
        assert response["model_calls"] == 0
        assert response["write_proposals"] == 0
        draft_block = _draft_block(response)
        assert draft_block["missing_fields"] == expected_missing[index]
        assert draft_block["fields"].get("reason") == "看病"
        if index >= 1:
            assert draft_block["fields"].get("leave_type_code") == "compensatory"
        text_block = next(
            block["text"]
            for block in response["blocks"]
            if block["type"] == "text"
        )
        assert "请假原因" not in text_block
        draft = db.scalar(select(AssistantFlowDraft).where(
            AssistantFlowDraft.owner_user_id == owner.id,
            AssistantFlowDraft.module_key == "hr",
            AssistantFlowDraft.conversation_id == conversation_id,
        ))
        assert draft is not None
        versions.append(draft.version)
        assert db.scalar(select(func.count(AssistantFlowDraft.id)).where(
            AssistantFlowDraft.owner_user_id == owner.id,
            AssistantFlowDraft.module_key == "hr",
            AssistantFlowDraft.conversation_id == conversation_id,
        )) == 1
        assert db.scalar(select(func.count(LeaveRequest.id))) == before_requests

    final = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        "2033年",
    )

    assert versions == sorted(versions)
    assert len(set(versions)) == 3
    assert final["slot_extraction_calls"] == 1
    assert db.scalar(select(func.count(ToolConfirmation.id))) == (
        before_confirmations + 1
    )
    assert db.scalar(select(func.count(LeaveRequest.id))) == before_requests
    confirmation = next(
        block for block in final["blocks"] if block["type"] == "confirmation"
    )
    assert confirmation["preview"] == {
        **confirmation["preview"],
        "leave_type_code": "compensatory",
        "start_date": "2033-09-20",
        "end_date": "2033-09-22",
        "reason": "看病",
    }
    assert extractor.inputs == [
        (turn_text, HR_SLOT_SCHEMA.provider_json)
        for turn_text in [*turns, "2033年"]
    ]
    assert planner.required_tool_names_seen == [
        "hr_get_my_leave_balances",
        "hr_calculate_leave_duration",
        "hr_submit_leave_request",
    ]


def test_hr_exact_clear_control_skips_extraction_and_planner(
    runtime_session: tuple[Session, dict[str, object]],
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    slot_service, extractor = _hr_slot_service([SlotExtractionEnvelope(
        schema_version=HR_SLOT_SCHEMA.version,
        candidates=[SlotCandidate(
            slot_name="leave_type_code",
            raw_value="年假",
            source_quote="年假",
        )],
    )])
    runtime = HrRuntime(
        planner=ExplodingPlanner(),  # type: ignore[arg-type]
        slot_extraction=slot_service,
    )
    conversation_id = runtime.create_conversation(
        db, owner.id, "clear control"
    )["id"]

    first = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        "我要请年假",
    )
    cleared = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        "清空草稿",
    )

    assert first["slot_extraction_calls"] == 1
    assert cleared["slot_extraction_calls"] == 0
    assert cleared["model_calls"] == 0
    assert len(extractor.inputs) == 1
    draft = db.scalar(select(AssistantFlowDraft).where(
        AssistantFlowDraft.owner_user_id == owner.id,
        AssistantFlowDraft.module_key == "hr",
        AssistantFlowDraft.conversation_id == conversation_id,
    ))
    assert draft is not None
    assert draft.status == DraftStatus.CLEARED.value


@pytest.mark.parametrize(
    ("control_text", "expected_reason"),
    (("使用新值", "就医"), ("保留原值", "探亲")),
)
def test_hr_exact_conflict_control_skips_extraction(
    runtime_session: tuple[Session, dict[str, object]],
    control_text: str,
    expected_reason: str,
) -> None:
    db, values = runtime_session
    owner = values["owner"]
    assert isinstance(owner, User)
    slot_service, extractor = _hr_slot_service([
        SlotExtractionEnvelope(
            schema_version=HR_SLOT_SCHEMA.version,
            candidates=[SlotCandidate(
                slot_name="reason",
                raw_value="探亲",
                source_quote="探亲",
            )],
        ),
        SlotExtractionEnvelope(
            schema_version=HR_SLOT_SCHEMA.version,
            candidates=[SlotCandidate(
                slot_name="reason",
                raw_value="就医",
                source_quote="就医",
            )],
        ),
    ])
    runtime = HrRuntime(
        planner=ExplodingPlanner(),  # type: ignore[arg-type]
        slot_extraction=slot_service,
    )
    conversation_id = runtime.create_conversation(
        db, owner.id, f"conflict control {control_text}"
    )["id"]

    runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        "我要请假，原因是探亲",
    )
    conflicted = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        "原因改为就医",
    )
    resolved = runtime.run_turn(
        db,
        owner.id,
        conversation_id,  # type: ignore[arg-type]
        uuid.uuid4(),
        control_text,
    )

    assert conflicted["slot_extraction_calls"] == 1
    assert resolved["slot_extraction_calls"] == 0
    assert resolved["model_calls"] == 0
    assert len(extractor.inputs) == 2
    draft = db.scalar(select(AssistantFlowDraft).where(
        AssistantFlowDraft.owner_user_id == owner.id,
        AssistantFlowDraft.module_key == "hr",
        AssistantFlowDraft.conversation_id == conversation_id,
        AssistantFlowDraft.status == DraftStatus.ACTIVE.value,
    ))
    assert draft is not None
    assert draft.field_values["reason"] == expected_reason
    assert "reason" not in draft.pending_candidates


def test_procurement_unknown_turn_ignores_action_only_active_draft(
    procurement_fixture: ProcurementFixture,
) -> None:
    slot_service, _extractor = _procurement_slot_service([
        SlotExtractionEnvelope(
            schema_version=PROCUREMENT_SLOT_SCHEMA.version,
            candidates=[],
        )
    ])
    planner = SequentialPlanner([
        PlannerTurn(text="请说明你想办理的采购事项。", tool_calls=())
    ])
    draft_store = AssistantDraftStore()
    observability = ProcurementObservability(ProcurementRepository())
    runtime = ProcurementRuntime(
        service=procurement_fixture.service(observability=observability),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=planner,  # type: ignore[arg-type]
        slot_extraction=slot_service,
        approval_runtime=object(),  # type: ignore[arg-type]
        observability=observability,
        draft_store=draft_store,
    )

    engine = procurement_fixture.sessions.kw["bind"]
    with engine.connect() as connection:
        outer_transaction = connection.begin()
        db = Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            conversation_id = runtime.create_conversation(
                db,
                procurement_fixture.applicant_id,
                "action-only draft must not activate",
            )["id"]
            draft_store.save(
                db,
                owner_user_id=procurement_fixture.applicant_id,
                module_key="procurement",
                conversation_id=conversation_id,  # type: ignore[arg-type]
                intent="draft_request",
                fields={"request_id": str(uuid.uuid4())},
                sources={},
                pending={},
            )
            db.commit()

            result = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                "随便看看",
            )

            assert result["text"] == "请说明你想办理的采购事项。"
            assert result["model_calls"] == 1
            assert result["slot_extraction_calls"] == 1
            assert not any(
                block["type"] == "assistant_draft"
                for block in result["blocks"]
            )
        finally:
            db.close()
            if outer_transaction.is_active:
                outer_transaction.rollback()


def test_procurement_follow_ups_merge_only_missing_fields_until_explicit_submit(
    procurement_fixture: ProcurementFixture,
) -> None:
    slot_service, extractor = _procurement_slot_service([
        SlotExtractionEnvelope(
            schema_version=PROCUREMENT_SLOT_SCHEMA.version,
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "椅子",
                    "specification": None,
                    "quantity": "三",
                    "unit": "把",
                    "estimated_unit_price": "500",
                    "category_hint": "办公用品",
                },
                source_quote="办公用品类三把单价500的椅子",
            )],
        ),
        SlotExtractionEnvelope(
            schema_version=PROCUREMENT_SLOT_SCHEMA.version,
            candidates=[
                SlotCandidate(
                    slot_name="title",
                    raw_value="会议室座椅",
                    source_quote="标题是会议室座椅",
                ),
                SlotCandidate(
                    slot_name="purpose",
                    raw_value="会议室扩容",
                    source_quote="用途是会议室扩容",
                ),
                SlotCandidate(
                    slot_name="needed_by_date",
                    raw_value="2030-09-20",
                    source_quote="需要日期2030-09-20",
                ),
                SlotCandidate(
                    slot_name="currency",
                    raw_value="人民币",
                    source_quote="币种人民币",
                ),
            ],
        ),
    ])
    planner = SequentialPlanner([
        PlannerTurn(
            text=None,
            tool_calls=(
                PlannedToolCall(
                    call_id="calculate-from-draft",
                    name="procurement_calculate_request_total",
                    arguments={},
                ),
            ),
        ),
        PlannerTurn(
            text=None,
            tool_calls=(
                PlannedToolCall(
                    call_id="submit-from-draft",
                    name="procurement_submit_request",
                    arguments={},
                ),
            ),
        ),
    ])
    observability = ProcurementObservability(ProcurementRepository())
    runtime = ProcurementRuntime(
        service=procurement_fixture.service(observability=observability),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=planner,  # type: ignore[arg-type]
        slot_extraction=slot_service,
        approval_runtime=object(),  # type: ignore[arg-type]
        observability=observability,
    )

    engine = procurement_fixture.sessions.kw["bind"]
    with engine.connect() as connection:
        outer_transaction = connection.begin()
        db = Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            conversation_id = runtime.create_conversation(
                db, procurement_fixture.applicant_id, "增量补充采购信息"
            )["id"]
            first_turn_id = uuid.uuid4()
            first = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                first_turn_id,
                "我想买办公用品类三把单价500的椅子",
            )
            first_draft = _draft_block(first)
            assert first["role"] == "assistant"
            assert first["request_content"] == "我想买办公用品类三把单价500的椅子"
            assert first["text"] == (
                "已记录本会话中你明确提供的采购信息，请只补充："
                "申请标题、采购用途、需要日期、币种。无需重复已经提供的内容。"
            )
            assert first_draft["fields"]["items"] == [{
                "category_code": "office_supplies",
                "item_name": "椅子",
                "specification": None,
                "quantity": "3",
                "unit": "把",
                "estimated_unit_price": "500",
            }]
            assert first["slot_extraction_calls"] == 1
            assert planner.tools_seen == []

            stored_first = db.scalar(select(AssistantTurn).where(
                AssistantTurn.owner_user_id == procurement_fixture.applicant_id,
                AssistantTurn.client_turn_id == first_turn_id,
            ))
            assert stored_first is not None
            assert stored_first.request_content == (
                "我想买办公用品类三把单价500的椅子"
            )
            assert stored_first.content == first["text"]

            second = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                "标题是会议室座椅，用途是会议室扩容，需要日期2030-09-20，币种人民币",
            )
            second_draft = _draft_block(second)
            assert second_draft["missing_fields"] == []
            assert second["request_content"] == (
                "标题是会议室座椅，用途是会议室扩容，"
                "需要日期2030-09-20，币种人民币"
            )
            assert second["text"] == (
                "采购信息已齐全。如要办理，请发送“提交这份采购申请”；"
                "在你明确提交前不会生成确认卡。"
            )
            assert "采购明细" not in second["text"]
            assert second["slot_extraction_calls"] == 1
            assert planner.tools_seen == []
            assert db.scalar(select(func.count(ProcurementRequest.id))) == 0
            assert db.scalar(select(func.count(ToolConfirmation.id))) == 0

            replay = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                first_turn_id,
                "我想买办公用品类三把单价500的椅子",
            )
            assert replay["replayed"] is True
            assert replay["role"] == "assistant"
            assert replay["request_content"] == "我想买办公用品类三把单价500的椅子"
            assert replay["text"] == first["text"]

            submitted = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                "提交这份采购申请",
            )
            assert db.scalar(select(func.count(ProcurementRequest.id))) == 0
            assert db.scalar(select(func.count(ToolConfirmation.id))) == 1
            confirmation = next(
                block
                for block in submitted["blocks"]
                if block["type"] == "confirmation"
            )
            assert confirmation["preview"]["title"] == "会议室座椅"
            assert confirmation["preview"]["purpose"] == "会议室扩容"
            assert confirmation["preview"]["needed_by_date"] == "2030-09-20"
            assert confirmation["preview"]["currency"] == "CNY"
            assert submitted["slot_extraction_calls"] == 0
            assert extractor.inputs == [
                (
                    "我想买办公用品类三把单价500的椅子",
                    PROCUREMENT_SLOT_SCHEMA.provider_json,
                ),
                (
                    "标题是会议室座椅，用途是会议室扩容，"
                    "需要日期2030-09-20，币种人民币",
                    PROCUREMENT_SLOT_SCHEMA.provider_json,
                ),
            ]
            assert planner.required_tool_names_seen == [
                "procurement_calculate_request_total",
                "procurement_submit_request",
            ]
            draft = db.scalar(select(AssistantFlowDraft).where(
                AssistantFlowDraft.owner_user_id == procurement_fixture.applicant_id,
                AssistantFlowDraft.module_key == "procurement",
                AssistantFlowDraft.conversation_id == conversation_id,
            ))
            assert draft is not None
            assert draft.status == DraftStatus.CLOSED.value
        finally:
            db.close()
            if outer_transaction.is_active:
                outer_transaction.rollback()


def test_procurement_unknown_intent_activates_partial_item_and_merges_only_missing_leaves(
    procurement_fixture: ProcurementFixture,
) -> None:
    first_text = "标题为办公用品，买一个桌子，单价600"
    assert ProcurementToolFlowPolicy._classify_intent(first_text) == "unknown"
    slot_service, extractor = _procurement_slot_service([
        SlotExtractionEnvelope(
            schema_version=PROCUREMENT_SLOT_SCHEMA.version,
            candidates=[
                SlotCandidate(
                    slot_name="title",
                    raw_value="办公用品",
                    source_quote="标题为办公用品",
                ),
                SlotCandidate(
                    slot_name="items",
                    raw_value={
                        "item_name": "桌子",
                        "specification": None,
                        "quantity": "一",
                        "unit": None,
                        "estimated_unit_price": "600",
                        "category_hint": None,
                    },
                    source_quote="买一个桌子，单价600",
                ),
            ],
        ),
        SlotExtractionEnvelope(
            schema_version=PROCUREMENT_SLOT_SCHEMA.version,
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "specification": None,
                    "unit": "张",
                    "category_hint": "办公用品",
                },
                source_quote="计量单位是张，品类是办公用品",
            )],
        ),
    ])
    observability = ProcurementObservability(ProcurementRepository())
    runtime = ProcurementRuntime(
        service=procurement_fixture.service(observability=observability),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=ExplodingPlanner(),  # type: ignore[arg-type]
        slot_extraction=slot_service,
        approval_runtime=object(),  # type: ignore[arg-type]
        observability=observability,
    )

    engine = procurement_fixture.sessions.kw["bind"]
    with engine.connect() as connection:
        outer_transaction = connection.begin()
        db = Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            conversation_id = runtime.create_conversation(
                db,
                procurement_fixture.applicant_id,
                "未知意图也能激活采购草稿",
            )["id"]
            first = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                first_text,
            )

            first_draft = _draft_block(first)
            assert first["model_calls"] == 0
            assert first["read_calls"] == 0
            assert first["write_proposals"] == 0
            assert first_draft["fields"]["title"] == "办公用品"
            assert "items" not in first_draft["fields"]
            assert first_draft["missing_fields"] == [
                "purpose",
                "needed_by_date",
                "currency",
                "items[0].unit",
                "items[0].category_code",
            ]

            second = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                "计量单位是张，品类是办公用品",
            )
            second_draft = _draft_block(second)
            assert second["model_calls"] == 0
            assert second["read_calls"] == 0
            assert second["write_proposals"] == 0
            assert second_draft["fields"]["items"] == [{
                "category_code": "office_supplies",
                "item_name": "桌子",
                "specification": None,
                "quantity": "1",
                "unit": "张",
                "estimated_unit_price": "600",
            }]
            assert second_draft["missing_fields"] == [
                "purpose",
                "needed_by_date",
                "currency",
            ]
            assert extractor.inputs == [
                (first_text, PROCUREMENT_SLOT_SCHEMA.provider_json),
                (
                    "计量单位是张，品类是办公用品",
                    PROCUREMENT_SLOT_SCHEMA.provider_json,
                ),
            ]
            assert db.scalar(select(func.count(ProcurementRequest.id))) == 0
            assert db.scalar(select(func.count(ApprovalInstance.id))) == 0
            assert db.scalar(select(func.count(ApprovalTask.id))) == 0
            assert db.scalar(select(func.count(ToolConfirmation.id))) == 0
        finally:
            db.close()
            if outer_transaction.is_active:
                outer_transaction.rollback()


def test_exact_procurement_item_reconciliation_preserves_trusted_item_across_turns(
    procurement_fixture: ProcurementFixture,
) -> None:
    turns = [
        "我想买10支笔，单价五元，9.20要",
        "标题办公用笔，年份2026 人民币，品类为办公用品",
        "用于办公室",
    ]
    slot_service, extractor = _procurement_slot_service(
        [
            SlotExtractionEnvelope(
                schema_version=PROCUREMENT_SLOT_SCHEMA.version,
                candidates=[
                    SlotCandidate(
                        slot_name="needed_by_date",
                        raw_value="9.20",
                        source_quote="9.20要",
                    ),
                    SlotCandidate(
                        slot_name="items",
                        raw_value={
                            "item_name": "笔",
                            "specification": None,
                            "quantity": "10",
                            "unit": "支",
                            "estimated_unit_price": "五元",
                            "category_hint": None,
                        },
                        source_quote="10支笔，单价五元",
                    ),
                ],
            ),
            SlotExtractionEnvelope(
                schema_version=PROCUREMENT_SLOT_SCHEMA.version,
                candidates=[
                    SlotCandidate(
                        slot_name="title",
                        raw_value="办公用笔",
                        source_quote="标题办公用笔",
                    ),
                    SlotCandidate(
                        slot_name="needed_by_year",
                        raw_value="年份2026",
                        source_quote="年份2026",
                    ),
                    SlotCandidate(
                        slot_name="currency",
                        raw_value="人民币",
                        source_quote="人民币",
                    ),
                    SlotCandidate(
                        slot_name="items",
                        raw_value={
                            "item_name": "笔",
                            "specification": None,
                            "unit": None,
                            "category_hint": "办公用品",
                        },
                        source_quote="品类为办公用品",
                    ),
                ],
            ),
            SlotExtractionEnvelope(
                schema_version=PROCUREMENT_SLOT_SCHEMA.version,
                candidates=[SlotCandidate(
                    slot_name="purpose",
                    raw_value="办公室",
                    source_quote="用于办公室",
                )],
            ),
        ],
        now_provider=lambda: datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    planner = SequentialPlanner([
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="calculate-reconciled-pen-draft",
                name="procurement_calculate_request_total",
                arguments={},
            ),),
        ),
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="submit-reconciled-pen-draft",
                name="procurement_submit_request",
                arguments={},
            ),),
        ),
    ])
    observability = ProcurementObservability(ProcurementRepository())
    runtime = ProcurementRuntime(
        service=procurement_fixture.service(observability=observability),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=planner,  # type: ignore[arg-type]
        slot_extraction=slot_service,
        approval_runtime=object(),  # type: ignore[arg-type]
        observability=observability,
    )

    engine = procurement_fixture.sessions.kw["bind"]
    with engine.connect() as connection:
        outer_transaction = connection.begin()
        db = Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            conversation_id = runtime.create_conversation(
                db,
                procurement_fixture.applicant_id,
                "采购明细拒绝候选不得污染可信草稿",
            )["id"]
            first = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                turns[0],
            )
            first_draft = _draft_block(first)
            assert first_draft["missing_fields"] == [
                "title",
                "purpose",
                "needed_by_year",
                "currency",
                "items[0].category_code",
            ]
            assert first["slot_extraction_calls"] == 1
            assert first["model_calls"] == 0

            second = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                turns[1],
            )
            second_draft = _draft_block(second)
            assert second_draft["missing_fields"] == ["purpose"]
            assert second_draft["fields"]["items"] == [{
                "category_code": "office_supplies",
                "item_name": "笔",
                "specification": None,
                "quantity": "10",
                "unit": "支",
                "estimated_unit_price": "5",
            }]
            assert "待补充信息" not in second["text"]
            assert second["slot_extraction_calls"] == 1
            assert second["model_calls"] == 0

            third = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                turns[2],
            )
            third_draft = _draft_block(third)
            assert third_draft["missing_fields"] == []
            assert third["text"] == (
                "采购信息已齐全。如要办理，请发送“提交这份采购申请”；"
                "在你明确提交前不会生成确认卡。"
            )
            assert third["slot_extraction_calls"] == 1
            assert third["model_calls"] == 0
            assert db.scalar(select(func.count(ProcurementRequest.id))) == 0
            assert db.scalar(select(func.count(ApprovalInstance.id))) == 0
            assert db.scalar(select(func.count(ApprovalTask.id))) == 0
            assert db.scalar(select(func.count(ToolConfirmation.id))) == 0

            submitted = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                "提交这份采购申请",
            )
            confirmation = next(
                block
                for block in submitted["blocks"]
                if block["type"] == "confirmation"
            )
            assert confirmation["preview"] == {
                **confirmation["preview"],
                "title": "办公用笔",
                "purpose": "办公室",
                "needed_by_date": "2026-09-20",
                "currency": "CNY",
                "item_count": 1,
            }
            assert db.scalar(select(func.count(ToolConfirmation.id))) == 1
            assert db.scalar(select(func.count(ProcurementRequest.id))) == 0
            assert db.scalar(select(func.count(ApprovalInstance.id))) == 0
            assert db.scalar(select(func.count(ApprovalTask.id))) == 0
            assert extractor.inputs == [
                (turns[0], PROCUREMENT_SLOT_SCHEMA.provider_json),
                (turns[1], PROCUREMENT_SLOT_SCHEMA.provider_json),
                (turns[2], PROCUREMENT_SLOT_SCHEMA.provider_json),
            ]
            assert planner.required_tool_names_seen == [
                "procurement_calculate_request_total",
                "procurement_submit_request",
            ]
            draft = db.scalar(select(AssistantFlowDraft).where(
                AssistantFlowDraft.owner_user_id == procurement_fixture.applicant_id,
                AssistantFlowDraft.module_key == "procurement",
                AssistantFlowDraft.conversation_id == conversation_id,
            ))
            assert draft is not None
            assert draft.status == DraftStatus.CLOSED.value
        finally:
            db.close()
            if outer_transaction.is_active:
                outer_transaction.rollback()


def test_procurement_ambiguous_zero_leaf_item_blocks_same_turn_proposal(
    procurement_fixture: ProcurementFixture,
) -> None:
    complete_quote = "办公用品椅子一把单价500元"
    repeated_quote = "办公用品桌子一个单价600元"
    text = (
        "请提交一份采购申请，标题是办公采购，用途是补充工位，"
        "需要日期2030-09-20，"
        f"币种人民币，{complete_quote}；{repeated_quote}；再次说明：{repeated_quote}"
    )
    assert ProcurementToolFlowPolicy._classify_intent(text) == "submit_request"
    slot_service, extractor = _procurement_slot_service([
        SlotExtractionEnvelope(
            schema_version=PROCUREMENT_SLOT_SCHEMA.version,
            candidates=[
                SlotCandidate(
                    slot_name="title",
                    raw_value="办公采购",
                    source_quote="标题是办公采购",
                ),
                SlotCandidate(
                    slot_name="purpose",
                    raw_value="补充工位",
                    source_quote="用途是补充工位",
                ),
                SlotCandidate(
                    slot_name="needed_by_date",
                    raw_value="2030-09-20",
                    source_quote="需要日期2030-09-20",
                ),
                SlotCandidate(
                    slot_name="currency",
                    raw_value="人民币",
                    source_quote="币种人民币",
                ),
                SlotCandidate(
                    slot_name="items",
                    raw_value={
                        "item_name": "椅子",
                        "specification": None,
                        "quantity": "一",
                        "unit": "把",
                        "estimated_unit_price": "500",
                        "category_hint": "办公用品",
                    },
                    source_quote=complete_quote,
                ),
                SlotCandidate(
                    slot_name="items",
                    raw_value={
                        "item_name": "桌子",
                        "specification": None,
                        "quantity": "一",
                        "unit": "个",
                        "estimated_unit_price": "600",
                        "category_hint": "办公用品",
                    },
                    source_quote=repeated_quote,
                ),
            ],
        ),
    ])
    observability = ProcurementObservability(ProcurementRepository())
    runtime = ProcurementRuntime(
        service=procurement_fixture.service(observability=observability),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=ExplodingPlanner(),  # type: ignore[arg-type]
        slot_extraction=slot_service,
        approval_runtime=object(),  # type: ignore[arg-type]
        observability=observability,
    )

    engine = procurement_fixture.sessions.kw["bind"]
    with engine.connect() as connection:
        outer_transaction = connection.begin()
        db = Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            conversation_id = runtime.create_conversation(
                db,
                procurement_fixture.applicant_id,
                "歧义采购明细必须阻止 proposal",
            )["id"]
            response = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                text,
            )

            draft = _draft_block(response)
            assert response["model_calls"] == 0
            assert response["read_calls"] == 0
            assert response["write_proposals"] == 0
            assert response["slot_extraction_calls"] == 1
            assert draft["missing_fields"] == ["items[1]"]
            assert len(extractor.inputs) == 1
            assert db.scalar(select(func.count(ProcurementRequest.id))) == 0
            assert db.scalar(select(func.count(ApprovalInstance.id))) == 0
            assert db.scalar(select(func.count(ApprovalTask.id))) == 0
            assert db.scalar(select(func.count(ToolConfirmation.id))) == 0
        finally:
            db.close()
            if outer_transaction.is_active:
                outer_transaction.rollback()


def test_procurement_fully_rejected_item_blocker_survives_later_exact_submit(
    procurement_fixture: ProcurementFixture,
) -> None:
    complete_quote = "办公用品椅子一把单价500元"
    rejected_quote = "另一个物品单价大概六百块"
    first_text = (
        "标题是办公采购，用途是补充工位，需要日期2030-09-20，"
        f"币种人民币，{complete_quote}；{rejected_quote}"
    )
    slot_service, extractor = _procurement_slot_service([
        SlotExtractionEnvelope(
            schema_version=PROCUREMENT_SLOT_SCHEMA.version,
            candidates=[
                SlotCandidate(
                    slot_name="title",
                    raw_value="办公采购",
                    source_quote="标题是办公采购",
                ),
                SlotCandidate(
                    slot_name="purpose",
                    raw_value="补充工位",
                    source_quote="用途是补充工位",
                ),
                SlotCandidate(
                    slot_name="needed_by_date",
                    raw_value="2030-09-20",
                    source_quote="需要日期2030-09-20",
                ),
                SlotCandidate(
                    slot_name="currency",
                    raw_value="人民币",
                    source_quote="币种人民币",
                ),
                SlotCandidate(
                    slot_name="items",
                    raw_value={
                        "item_name": "椅子",
                        "specification": None,
                        "quantity": "一",
                        "unit": "把",
                        "estimated_unit_price": "500",
                        "category_hint": "办公用品",
                    },
                    source_quote=complete_quote,
                ),
                SlotCandidate(
                    slot_name="items",
                    raw_value={
                        "estimated_unit_price": "六百",
                        "unit": None,
                        "category_hint": None,
                    },
                    source_quote=rejected_quote,
                ),
            ],
        ),
        SlotExtractionEnvelope(
            schema_version=PROCUREMENT_SLOT_SCHEMA.version,
            candidates=[],
        ),
    ])
    observability = ProcurementObservability(ProcurementRepository())
    runtime = ProcurementRuntime(
        service=procurement_fixture.service(observability=observability),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=ExplodingPlanner(),  # type: ignore[arg-type]
        slot_extraction=slot_service,
        approval_runtime=object(),  # type: ignore[arg-type]
        observability=observability,
    )

    engine = procurement_fixture.sessions.kw["bind"]
    with engine.connect() as connection:
        outer_transaction = connection.begin()
        db = Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            conversation_id = runtime.create_conversation(
                db,
                procurement_fixture.applicant_id,
                "零叶 rejected blocker 跨轮保留",
            )["id"]
            first = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                first_text,
            )
            assert _draft_block(first)["missing_fields"] == ["items[1]"]

            submitted = runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation_id,  # type: ignore[arg-type]
                uuid.uuid4(),
                "提交这份采购申请",
            )

            assert submitted["model_calls"] == 0
            assert submitted["read_calls"] == 0
            assert submitted["write_proposals"] == 0
            assert submitted["slot_extraction_calls"] == 1
            assert _draft_block(submitted)["missing_fields"] == ["items[1]"]
            assert len(extractor.inputs) == 2
            assert db.scalar(select(func.count(ProcurementRequest.id))) == 0
            assert db.scalar(select(func.count(ApprovalInstance.id))) == 0
            assert db.scalar(select(func.count(ApprovalTask.id))) == 0
            assert db.scalar(select(func.count(ToolConfirmation.id))) == 0
        finally:
            db.close()
            if outer_transaction.is_active:
                outer_transaction.rollback()


def test_procurement_extraction_failure_preserves_draft_and_creates_no_resources(
    procurement_fixture: ProcurementFixture,
) -> None:
    slot_service, extractor = _procurement_slot_service([
        SlotExtractionEnvelope(
            schema_version=PROCUREMENT_SLOT_SCHEMA.version,
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "椅子",
                    "specification": None,
                    "quantity": "三",
                    "unit": "把",
                    "estimated_unit_price": "500",
                    "category_hint": None,
                },
                source_quote="三把单价500的椅子",
            )],
        ),
        SlotExtractionError("slot_extraction_timeout", retryable=True),
    ])
    observability = ProcurementObservability(ProcurementRepository())
    runtime = ProcurementRuntime(
        service=procurement_fixture.service(observability=observability),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=ExplodingPlanner(),  # type: ignore[arg-type]
        slot_extraction=slot_service,
        approval_runtime=object(),  # type: ignore[arg-type]
        observability=observability,
    )

    with procurement_fixture.sessions() as db:
        conversation_id = runtime.create_conversation(
            db, procurement_fixture.applicant_id, "procurement fail closed"
        )["id"]
        first = runtime.run_turn(
            db,
            procurement_fixture.applicant_id,
            conversation_id,  # type: ignore[arg-type]
            uuid.uuid4(),
            "我想买三把单价500的椅子",
        )
        draft = db.scalar(select(AssistantFlowDraft).where(
            AssistantFlowDraft.owner_user_id == procurement_fixture.applicant_id,
            AssistantFlowDraft.module_key == "procurement",
            AssistantFlowDraft.conversation_id == conversation_id,
            AssistantFlowDraft.status == DraftStatus.ACTIVE.value,
        ))
        assert draft is not None
        before_draft = (
            dict(draft.field_values),
            dict(draft.pending_candidates),
            draft.version,
        )
        baseline = tuple(
            db.scalar(select(func.count()).select_from(model)) or 0
            for model in (
                ProcurementRequest,
                ApprovalInstance,
                ApprovalTask,
                ToolConfirmation,
            )
        )
        failed_turn_id = uuid.uuid4()

        failed = runtime.run_turn(
            db,
            procurement_fixture.applicant_id,
            conversation_id,  # type: ignore[arg-type]
            failed_turn_id,
            "标题是办公椅",
        )
        replay = runtime.run_turn(
            db,
            procurement_fixture.applicant_id,
            conversation_id,  # type: ignore[arg-type]
            failed_turn_id,
            "标题是办公椅",
        )
        db.refresh(draft)

        assert first["slot_extraction_calls"] == 1
        assert len(extractor.inputs) == 2
        assert failed["slot_extraction_calls"] == 1
        assert replay["slot_extraction_calls"] == 0
        assert failed["model_calls"] == 0
        assert failed["read_calls"] == 0
        assert failed["write_proposals"] == 0
        assert any(
            block["type"] == "error"
            and block["code"] == "slot_extraction_timeout"
            for block in failed["blocks"]
        )
        assert (
            dict(draft.field_values),
            dict(draft.pending_candidates),
            draft.version,
        ) == before_draft
        assert tuple(
            db.scalar(select(func.count()).select_from(model)) or 0
            for model in (
                ProcurementRequest,
                ApprovalInstance,
                ApprovalTask,
                ToolConfirmation,
            )
        ) == baseline


@pytest.mark.parametrize("initial_status", ["reserved", "dispatched"])
def test_procurement_stale_extraction_never_redispatches_or_creates_resources(
    procurement_fixture: ProcurementFixture,
    initial_status: str,
) -> None:
    service, extractor = _procurement_slot_service([])
    runtime = ProcurementRuntime(
        service=procurement_fixture.service(),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=ExplodingPlanner(),  # type: ignore[arg-type]
        slot_extraction=service,
        approval_runtime=object(),  # type: ignore[arg-type]
    )
    old_now = datetime.now(timezone.utc) - timedelta(minutes=5)

    with procurement_fixture.sessions() as db:
        conversation_id = runtime.create_conversation(
            db, procurement_fixture.applicant_id, f"stale {initial_status}"
        )["id"]
        client_turn_id = uuid.uuid4()
        text = "我想采购办公椅"
        request = SlotExtractionPreparationRequest(
            owner_user_id=procurement_fixture.applicant_id,
            module_key="procurement",
            conversation_id=conversation_id,  # type: ignore[arg-type]
            client_turn_id=client_turn_id,
            current_user_turn_text=text,
            slot_schema=PROCUREMENT_SLOT_SCHEMA,
        )
        claim = service.repository.claim(
            db,
            request.identity(),
            service.fingerprinter.fingerprint(request.identity()),
            model_name=extractor.model,
            now=old_now,
            timeout_seconds=1,
        )
        if initial_status == "dispatched":
            service.repository.mark_dispatched(
                db,
                claim.operation.id,
                now=old_now,
                timeout_seconds=1,
            )
        db.commit()
        baseline = tuple(
            db.scalar(select(func.count()).select_from(model)) or 0
            for model in (ProcurementRequest, ApprovalInstance, ApprovalTask)
        )

        response = runtime.run_turn(
            db,
            procurement_fixture.applicant_id,
            conversation_id,  # type: ignore[arg-type]
            client_turn_id,
            text,
        )
        db.refresh(claim.operation)

        assert extractor.inputs == []
        assert response["slot_extraction_calls"] == 0
        assert response["model_calls"] == 0
        assert any(
            block["type"] == "error"
            and block["code"] == "slot_extraction_outcome_indeterminate"
            for block in response["blocks"]
        )
        assert claim.operation.status == SlotExtractionOperationStatus.INDETERMINATE
        assert tuple(
            db.scalar(select(func.count()).select_from(model)) or 0
            for model in (ProcurementRequest, ApprovalInstance, ApprovalTask)
        ) == baseline


def test_procurement_draft_version_conflict_blocks_confirmation_and_resources(
    procurement_fixture: ProcurementFixture,
) -> None:
    class ConflictingDraftStore(AssistantDraftStore):
        def __init__(self) -> None:
            super().__init__()
            self.assertions = 0

        def assert_current_version(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.assertions += 1
            raise ToolError("assistant_draft_version_conflict")

    service, extractor = _procurement_slot_service([SlotExtractionEnvelope(
        schema_version=PROCUREMENT_SLOT_SCHEMA.version,
        candidates=[
            SlotCandidate(
                slot_name="title",
                raw_value="办公椅",
                source_quote="标题是办公椅",
            ),
            SlotCandidate(
                slot_name="purpose",
                raw_value="办公室扩容",
                source_quote="用途是办公室扩容",
            ),
            SlotCandidate(
                slot_name="needed_by_date",
                raw_value="2030-09-20",
                source_quote="需要日期2030-09-20",
            ),
            SlotCandidate(
                slot_name="currency",
                raw_value="人民币",
                source_quote="币种人民币",
            ),
            SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "椅子",
                    "specification": None,
                        "quantity": "1",
                        "unit": "把",
                        "estimated_unit_price": "500",
                        "category_hint": "办公用品",
                    },
                    source_quote="办公用品类1把单价500的椅子",
            ),
        ],
    )])
    planner = SequentialPlanner([
        PlannerTurn(None, (PlannedToolCall(
            call_id="calculate-version",
            name="procurement_calculate_request_total",
            arguments={},
        ),)),
        PlannerTurn(None, (PlannedToolCall(
            call_id="submit-version",
            name="procurement_submit_request",
            arguments={},
        ),)),
    ])
    draft_store = ConflictingDraftStore()
    runtime = ProcurementRuntime(
        service=procurement_fixture.service(),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=planner,  # type: ignore[arg-type]
        slot_extraction=service,
        approval_runtime=object(),  # type: ignore[arg-type]
        draft_store=draft_store,
    )

    with procurement_fixture.sessions() as db:
        conversation_id = runtime.create_conversation(
            db, procurement_fixture.applicant_id, "draft version conflict"
        )["id"]
        runtime.run_turn(
            db,
            procurement_fixture.applicant_id,
            conversation_id,  # type: ignore[arg-type]
            uuid.uuid4(),
            (
                    "我想买办公用品类1把单价500的椅子，标题是办公椅，"
                "用途是办公室扩容，需要日期2030-09-20，币种人民币"
            ),
        )
        baseline = tuple(
            db.scalar(select(func.count()).select_from(model)) or 0
            for model in (
                ProcurementRequest,
                ApprovalInstance,
                ApprovalTask,
                ToolConfirmation,
            )
        )

        response = runtime.run_turn(
            db,
            procurement_fixture.applicant_id,
            conversation_id,  # type: ignore[arg-type]
            uuid.uuid4(),
            "提交这份采购申请",
        )

        assert len(extractor.inputs) == 1
        assert draft_store.assertions == 1
        assert response["slot_extraction_calls"] == 0
        assert response["write_proposals"] == 0
        assert any(
            block["type"] == "error"
            and block["code"] == "assistant_draft_version_conflict"
            for block in response["blocks"]
        )
        assert tuple(
            db.scalar(select(func.count()).select_from(model)) or 0
            for model in (
                ProcurementRequest,
                ApprovalInstance,
                ApprovalTask,
                ToolConfirmation,
            )
        ) == baseline
