from __future__ import annotations

from datetime import date
from decimal import Decimal
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from policy_api.assistant_drafts.models import AssistantFlowDraft, DraftStatus
from policy_api.hr.enums import LeaveTypeCode, WorkCalendarDayKind
from policy_api.hr.models import (
    EmployeeProfile,
    LeaveAccount,
    LeaveRequest,
    LeaveType,
    WorkCalendarDay,
)
from policy_api.hr.runtime import HrRuntime
from policy_api.models import User
from policy_api.slot_extraction.client import SlotExtractionClient
from policy_api.slot_extraction.fingerprint import SlotExtractionFingerprinter
from policy_api.slot_extraction.models import (
    SlotExtractionOperation,
    SlotExtractionOperationStatus,
)
from policy_api.slot_extraction.repository import SlotExtractionOperationRepository
from policy_api.slot_extraction.service import SlotExtractionService
from policy_api.tools.models import ToolConfirmation
from policy_api.tools.probe_config import ProbeSettings
from policy_api.tools.types import PlannedToolCall, PlannerTurn
from tests.integration.test_assistant_draft_conversations import (
    SequentialPlanner,
    _draft_block,
)
from tests.integration.test_hr_runtime import runtime_session


def test_real_model_exact_sequence_activates_one_scoped_hr_draft(
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
        year=2026,
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
            date(2026, 9, 20),
            date(2026, 9, 21),
            date(2026, 9, 22),
        )
    ])
    db.commit()

    settings = ProbeSettings(_env_file=None)
    planner = SequentialPlanner([
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="real-balance-after-draft-activation",
                name="hr_get_my_leave_balances",
                arguments={},
            ),),
        ),
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="real-duration-after-draft-activation",
                name="hr_calculate_leave_duration",
                arguments={},
            ),),
        ),
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="real-submit-after-draft-activation",
                name="hr_submit_leave_request",
                arguments={},
            ),),
        ),
    ])
    with SlotExtractionClient(
        base_url=str(settings.model_base_url),
        api_key=settings.model_api_key.get_secret_value(),
        model=settings.chat_model,
        timeout=settings.model_timeout_seconds,
    ) as slot_client:
        runtime = HrRuntime(
            planner=planner,  # type: ignore[arg-type]
            slot_extraction=SlotExtractionService(
                client=slot_client,
                repository=SlotExtractionOperationRepository(),
                fingerprinter=SlotExtractionFingerprinter(
                    b"real-hr-draft-activation-evaluation-key"
                ),
                model_timeout_seconds=settings.model_timeout_seconds,
            ),
        )
        conversation_id = runtime.create_conversation(
            db,
            owner.id,
            "real-model HR draft activation evaluation",
        )["id"]
        before_requests = db.scalar(select(func.count(LeaveRequest.id)))
        before_confirmations = db.scalar(select(func.count(ToolConfirmation.id)))
        versions: list[int] = []
        exact_turns = [
            "我想在9.20-9.22请假 用于看病",
            "调休 用于看病",
            "9.20-9.22",
        ]
        expected_missing = [
            ["leave_type_code", "year"],
            ["year"],
            ["year"],
        ]

        for index, turn_text in enumerate(exact_turns):
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
                assert draft_block["fields"].get(
                    "leave_type_code"
                ) == "compensatory"
            assert "reason" not in draft_block["missing_fields"]
            assert db.scalar(select(func.count(LeaveRequest.id))) == before_requests
            draft = db.scalar(select(AssistantFlowDraft).where(
                AssistantFlowDraft.owner_user_id == owner.id,
                AssistantFlowDraft.module_key == "hr",
                AssistantFlowDraft.conversation_id == conversation_id,
            ))
            assert draft is not None
            versions.append(draft.version)

        final = runtime.run_turn(
            db,
            owner.id,
            conversation_id,  # type: ignore[arg-type]
            uuid.uuid4(),
            "2026年",
        )

    assert len(set(versions)) == 3
    assert versions == sorted(versions)
    assert final["slot_extraction_calls"] == 1
    assert db.scalar(select(func.count(ToolConfirmation.id))) == (
        before_confirmations + 1
    )
    assert db.scalar(select(func.count(LeaveRequest.id))) == before_requests
    confirmation = next(
        block for block in final["blocks"] if block["type"] == "confirmation"
    )
    assert {
        name: confirmation["preview"][name]
        for name in (
            "leave_type_code",
            "start_date",
            "end_date",
            "reason",
        )
    } == {
        "leave_type_code": "compensatory",
        "start_date": "2026-09-20",
        "end_date": "2026-09-22",
        "reason": "看病",
    }
    operations = db.scalars(select(SlotExtractionOperation).where(
        SlotExtractionOperation.owner_user_id == owner.id,
        SlotExtractionOperation.module_key == "hr",
        SlotExtractionOperation.conversation_id == conversation_id,
    )).all()
    assert len(operations) == 4
    assert all(
        operation.status == SlotExtractionOperationStatus.SUCCEEDED.value
        and operation.model_name == settings.chat_model
        and operation.accepted_count + operation.pending_count > 0
        for operation in operations
    )
    draft = db.scalar(select(AssistantFlowDraft).where(
        AssistantFlowDraft.owner_user_id == owner.id,
        AssistantFlowDraft.module_key == "hr",
        AssistantFlowDraft.conversation_id == conversation_id,
    ))
    assert draft is not None
    assert draft.status == DraftStatus.CLOSED.value
    print(
        "real_hr_draft_activation_status=passed "
        f"model={settings.chat_model} extraction_calls={len(operations)} "
        "write_executed=0 created_leave_requests=0 confirmations=1"
    )
