from __future__ import annotations

from datetime import date

from policy_api.evaluation.real_slot_extraction import SafeRealSlotExtractionEvaluator
from policy_api.evaluation.slot_extraction import (
    SlotExtractionCase,
    SlotExtractionExpectation,
)
from policy_api.procurement.slot_schema import PROCUREMENT_SLOT_SCHEMA
from policy_api.slot_extraction.schemas import SlotCandidate, SlotExtractionEnvelope


class FixtureClient:
    model = "deepseek-v4-flash"

    def __init__(self, envelope: SlotExtractionEnvelope) -> None:
        self.envelope = envelope
        self.dispatch_count = 0

    def extract(self, *, current_user_turn_text, slot_schema):  # type: ignore[no-untyped-def]
        del current_user_turn_text, slot_schema
        self.dispatch_count += 1
        return self.envelope


def test_safe_procurement_evaluation_reuses_validator_and_merger_without_writes() -> None:
    text = (
        "申请标题是办公椅 用途放在办公室 需要日期为2026.9.30 "
        "币种人民币 办公用品类明细办公椅三把单价500元"
    )
    envelope = SlotExtractionEnvelope(
        schema_version=PROCUREMENT_SLOT_SCHEMA.version,
        candidates=[
            SlotCandidate(
                slot_name="title", raw_value="办公椅", source_quote="标题是办公椅"
            ),
            SlotCandidate(
                slot_name="purpose", raw_value="放在办公室", source_quote="用途放在办公室"
            ),
            SlotCandidate(
                slot_name="needed_by_date",
                raw_value="2026.9.30",
                source_quote="需要日期为2026.9.30",
            ),
            SlotCandidate(
                slot_name="currency", raw_value="人民币", source_quote="币种人民币"
            ),
            SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "办公椅",
                    "specification": None,
                    "quantity": "三",
                    "unit": "把",
                    "estimated_unit_price": "500",
                    "category_hint": "办公用品",
                },
                source_quote="办公用品类明细办公椅三把单价500元",
            ),
        ],
    )
    case = SlotExtractionCase(
        id="SE-INTEGRATION-PROC-001",
        module="procurement",
        category="natural_multifield",
        current_user_turn=text,
        initial_draft={},
    )
    expected = SlotExtractionExpectation(
        case_id=case.id,
        module="procurement",
        category=case.category,
        applicable_metrics=[
            "envelope_valid", "field_precision", "field_recall",
            "source_verified", "terminal_correct", "must_not_execute",
            "call_budget_respected",
        ],
        expected_fields={
            "title": "办公椅",
            "purpose": "放在办公室",
            "needed_by_date": "2026-09-30",
            "currency": "CNY",
            "items": [{
                "category_code": "office_supplies",
                "item_name": "办公椅",
                "specification": None,
                "quantity": "3",
                "unit": "把",
                "estimated_unit_price": "500",
            }],
        },
        expected_pending={},
        expected_rejected=[],
        expected_terminal="accepted",
        must_not_execute=True,
    )
    client = FixtureClient(envelope)
    evaluator = SafeRealSlotExtractionEvaluator(
        client=client,
        reference_date=date(2026, 8, 28),
    )

    trace = evaluator.evaluate(case, expected)

    assert trace.canonical_fields == expected.expected_fields
    assert trace.pending == {}
    assert trace.rejected == ()
    assert trace.terminal == "accepted"
    assert trace.extraction_calls == 1
    assert client.dispatch_count == 1
    assert (
        trace.write_executed,
        trace.created_resources,
        trace.duplicate_resources,
    ) == (0, 0, 0)
