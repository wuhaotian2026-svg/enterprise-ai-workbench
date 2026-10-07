from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from policy_api.approvals.models import ApprovalInstance, ApprovalTask
from policy_api.assistant_drafts.models import AssistantFlowDraft, DraftStatus
from policy_api.procurement.models import ProcurementRequest
from policy_api.procurement.observability import ProcurementObservability
from policy_api.procurement.repository import ProcurementRepository
from policy_api.procurement.runtime import ProcurementRuntime
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
from policy_api.workbench.capabilities import CapabilityResolver
from tests.integration.test_assistant_draft_conversations import (
    SequentialPlanner,
    _draft_block,
)
from tests.integration.test_procurement_submission_transactions import (
    ProcurementFixture,
    procurement_fixture,
)


def test_real_model_exact_procurement_item_reconciliation(
    procurement_fixture: ProcurementFixture,
) -> None:
    settings = ProbeSettings(_env_file=None)
    planner = SequentialPlanner([
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="real-calculate-reconciled-pen-draft",
                name="procurement_calculate_request_total",
                arguments={},
            ),),
        ),
        PlannerTurn(
            text=None,
            tool_calls=(PlannedToolCall(
                call_id="real-submit-reconciled-pen-draft",
                name="procurement_submit_request",
                arguments={},
            ),),
        ),
    ])
    observability = ProcurementObservability(ProcurementRepository())
    exact_turns = [
        "我想买10支笔，单价五元，9.20要",
        "标题办公用笔，年份2026 人民币，品类为办公用品",
        "用于办公室",
    ]

    engine = procurement_fixture.sessions.kw["bind"]
    with engine.connect() as connection:
        outer_transaction = connection.begin()
        db = Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            with SlotExtractionClient(
                base_url=str(settings.model_base_url),
                api_key=settings.model_api_key.get_secret_value(),
                model=settings.chat_model,
                timeout=settings.model_timeout_seconds,
            ) as slot_client:
                runtime = ProcurementRuntime(
                    service=procurement_fixture.service(
                        observability=observability
                    ),
                    capability_resolver=CapabilityResolver(),
                    request_reader=object(),  # type: ignore[arg-type]
                    planner=planner,  # type: ignore[arg-type]
                    slot_extraction=SlotExtractionService(
                        client=slot_client,
                        repository=SlotExtractionOperationRepository(),
                        fingerprinter=SlotExtractionFingerprinter(
                            b"real-procurement-item-reconciliation-key"
                        ),
                        model_timeout_seconds=settings.model_timeout_seconds,
                        now_provider=lambda: datetime(
                            2026, 8, 31, tzinfo=timezone.utc
                        ),
                    ),
                    approval_runtime=object(),  # type: ignore[arg-type]
                    observability=observability,
                )
                conversation_id = runtime.create_conversation(
                    db,
                    procurement_fixture.applicant_id,
                    "real-model procurement item reconciliation evaluation",
                )["id"]
                before_requests = db.scalar(
                    select(func.count(ProcurementRequest.id))
                )
                before_instances = db.scalar(
                    select(func.count(ApprovalInstance.id))
                )
                before_tasks = db.scalar(select(func.count(ApprovalTask.id)))
                before_confirmations = db.scalar(
                    select(func.count(ToolConfirmation.id))
                )

                latest: dict[str, object] | None = None
                for text in exact_turns:
                    latest = runtime.run_turn(
                        db,
                        procurement_fixture.applicant_id,
                        conversation_id,  # type: ignore[arg-type]
                        uuid.uuid4(),
                        text,
                    )
                    assert latest["slot_extraction_calls"] == 1
                    assert latest["model_calls"] == 0
                    assert latest["write_proposals"] == 0
                    assert db.scalar(
                        select(func.count(ProcurementRequest.id))
                    ) == before_requests
                    assert db.scalar(
                        select(func.count(ApprovalInstance.id))
                    ) == before_instances
                    assert db.scalar(
                        select(func.count(ApprovalTask.id))
                    ) == before_tasks

                assert latest is not None
                draft_block = _draft_block(latest)
                assert draft_block["missing_fields"] == []
                assert draft_block["fields"]["items"] == [{
                    "category_code": "office_supplies",
                    "item_name": "笔",
                    "specification": None,
                    "quantity": "10",
                    "unit": "支",
                    "estimated_unit_price": "5",
                }]
                assert "待补充信息" not in latest["text"]

                final = runtime.run_turn(
                    db,
                    procurement_fixture.applicant_id,
                    conversation_id,  # type: ignore[arg-type]
                    uuid.uuid4(),
                    "提交这份采购申请",
                )

            assert final["slot_extraction_calls"] == 0
            assert db.scalar(select(func.count(ToolConfirmation.id))) == (
                before_confirmations + 1
            )
            assert db.scalar(select(func.count(ProcurementRequest.id))) == (
                before_requests
            )
            assert db.scalar(select(func.count(ApprovalInstance.id))) == (
                before_instances
            )
            assert db.scalar(select(func.count(ApprovalTask.id))) == before_tasks
            confirmation = next(
                block
                for block in final["blocks"]
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
            operations = db.scalars(select(SlotExtractionOperation).where(
                SlotExtractionOperation.owner_user_id
                == procurement_fixture.applicant_id,
                SlotExtractionOperation.module_key == "procurement",
                SlotExtractionOperation.conversation_id == conversation_id,
            )).all()
            assert len(operations) == 3
            assert all(
                operation.status
                == SlotExtractionOperationStatus.SUCCEEDED.value
                and operation.model_name == settings.chat_model
                and operation.accepted_count + operation.pending_count > 0
                for operation in operations
            )
            draft = db.scalar(select(AssistantFlowDraft).where(
                AssistantFlowDraft.owner_user_id
                == procurement_fixture.applicant_id,
                AssistantFlowDraft.module_key == "procurement",
                AssistantFlowDraft.conversation_id == conversation_id,
            ))
            assert draft is not None
            assert draft.status == DraftStatus.CLOSED.value
            print(
                "real_procurement_item_reconciliation_status=passed "
                f"model={settings.chat_model} "
                f"extraction_calls={len(operations)} confirmations=1 "
                "write_executed=0 created_resources=0"
            )
        finally:
            db.close()
            if outer_transaction.is_active:
                outer_transaction.rollback()
