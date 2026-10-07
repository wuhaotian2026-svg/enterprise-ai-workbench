from datetime import date
from typing import cast
from uuid import uuid4

from sqlalchemy.orm import Session

from policy_api.procurement.draft import merge_procurement_draft
from policy_api.procurement.slot_validation import validate_procurement_candidates
from policy_api.procurement.tool_flow_policy import build_procurement_tool_flow_policy
from policy_api.procurement.tools import (
    CalculateRequestTotalInput,
    build_procurement_tool_definitions,
)
from policy_api.models import UserRole
from policy_api.slot_extraction.controls import parse_draft_control
from policy_api.slot_extraction.merge import CandidateValidationResult, PendingCandidate
from policy_api.slot_extraction.schemas import SlotCandidate, SlotExtractionEnvelope
from policy_api.tools.definitions import ToolContext
from policy_api.tools.registry import ToolRegistry


def _complete_desk_item(
    *,
    specification: str | None,
    price: str = "600",
) -> dict[str, object]:
    return {
        "category_code": "office_supplies",
        "item_name": "桌子",
        "specification": specification,
        "quantity": "1",
        "unit": "个",
        "estimated_unit_price": price,
    }


def _complete_desk_validation(
    *,
    specification: str | None,
    price: str = "600",
    source_turn_id: str = "turn-2",
) -> CandidateValidationResult:
    specification_text = specification or ""
    text = (
        f"办公用品类{specification_text}桌子数量一，单位个，单价{price}元"
    )
    return validate_procurement_candidates(
        text=text,
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "桌子",
                    "specification": specification,
                    "quantity": "一",
                    "unit": "个",
                    "estimated_unit_price": price,
                    "category_hint": "办公用品类",
                },
                source_quote=text,
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id=source_turn_id,
    )


def _item_name_only_validation(
    item_name: str,
    *,
    source_turn_id: str = "turn-2",
) -> CandidateValidationResult:
    return validate_procurement_candidates(
        text=item_name,
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": item_name,
                    "specification": None,
                    "unit": None,
                    "category_hint": None,
                },
                source_quote=item_name,
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id=source_turn_id,
    )


def _validated_item_source(turn_id: str) -> dict[str, object]:
    return {
        "source_turn_id": turn_id,
        "source_kind": "user_explicit",
        "slot_schema_version": "slot-extraction-v1",
        "source_spans": [[0, 1]],
        "validator_version": "procurement-slot-validator-v6",
        "validation_status": "accepted",
        "match_kind": "original_exact",
    }


def test_natural_purchase_sentence_creates_safe_draft_item() -> None:
    validation = validate_procurement_candidates(
        text="我想买办公用品类三把单价500的椅子",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
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
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    result = merge_procurement_draft(
        fields={},
        pending={},
        sources={},
        validation=validation,
        control=None,
        source_turn_id="turn-1",
    )

    assert result.intent == "draft_request"
    assert result.explicit_submit is False
    assert result.fields["items"] == [
        {
            "category_code": "office_supplies",
            "item_name": "椅子",
            "specification": None,
            "quantity": "3",
            "unit": "把",
            "estimated_unit_price": "500",
        }
    ]
    assert result.missing_fields == ("title", "purpose", "needed_by_date", "currency")


def test_rejected_duplicate_leaf_does_not_invalidate_existing_canonical_item() -> None:
    first_text = "我想买10支笔，单价五元，9.20要"
    first_validation = validate_procurement_candidates(
        text=first_text,
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
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
            )],
        ),
        today=date(2026, 8, 31),
        source_turn_id="turn-1",
    )
    first = merge_procurement_draft(
        fields={}, pending={}, sources={}, validation=first_validation,
        control=None, source_turn_id="turn-1", today=date(2026, 8, 31),
    )

    category_turn = "标题办公用笔，年份2026 人民币，品类为办公用品"
    category_validation = validate_procurement_candidates(
        text=category_turn,
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "笔",
                    "specification": None,
                    "unit": None,
                    "category_hint": "办公用品",
                },
                source_quote="品类为办公用品",
            )],
        ),
        today=date(2026, 8, 31),
        source_turn_id="turn-2",
    )
    assert any(
        item.slot_name == "items[0].item_name"
        and item.reason_code == "raw_value_not_in_source_quote"
        for item in category_validation.rejected
    )

    result = merge_procurement_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=category_validation,
        control=None,
        source_turn_id="turn-2",
        today=date(2026, 8, 31),
    )

    assert result.fields["items"] == [{
        "category_code": "office_supplies",
        "item_name": "笔",
        "specification": None,
        "quantity": "10",
        "unit": "支",
        "estimated_unit_price": "5",
    }]
    assert "items" not in result.pending
    assert not any(name.startswith("items[") for name in result.missing_fields)


def test_lazy_normalization_promotes_stored_complete_item_with_stale_rejection() -> None:
    source = _validated_item_source("turn-1")
    stored_item = {
        "item_ref": "stored-pen",
        "fields": {
            "category_code": "office_supplies",
            "item_name": "笔",
            "quantity": "10",
            "unit": "支",
            "estimated_unit_price": "5",
        },
        "field_sources": {
            name: dict(source)
            for name in (
                "category_code",
                "item_name",
                "quantity",
                "unit",
                "estimated_unit_price",
            )
        },
        "missing_fields": [],
        "rejected_fields": [{
            "field_path": "items[0].item_name",
            "reason_code": "raw_value_not_in_source_quote",
        }],
    }
    pending = {
        "items": {
            "slot_name": "items",
            "reason_code": "item_fields_required",
            "canonical_fragment": {"items": [stored_item]},
            "provenance": dict(source),
        }
    }

    result = merge_procurement_draft(
        fields={
            "title": "办公用笔",
            "purpose": "办公室",
            "needed_by_date": "2026-09-20",
            "currency": "CNY",
        },
        pending=pending,
        sources={},
        validation=CandidateValidationResult(
            accepted={}, pending={}, rejected=()
        ),
        control=None,
        source_turn_id="turn-3",
        today=date(2026, 8, 31),
    )

    assert result.fields["items"] == [{
        "category_code": "office_supplies",
        "item_name": "笔",
        "specification": None,
        "quantity": "10",
        "unit": "支",
        "estimated_unit_price": "5",
    }]
    assert result.pending == {}
    assert result.missing_fields == ()


def test_procurement_follow_up_merges_only_explicit_fields() -> None:
    previous = {
        "items": [{
            "category_code": "office_supplies",
            "item_name": "椅子",
            "specification": None,
            "quantity": "3",
            "unit": "把",
            "estimated_unit_price": "500",
        }]
    }
    text = "标题是会议室座椅，用途是会议室扩容，需要日期2026-09-20，币种人民币"
    validation = validate_procurement_candidates(
        text=text,
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[
                SlotCandidate(slot_name="title", raw_value="会议室座椅", source_quote="标题是会议室座椅"),
                SlotCandidate(slot_name="purpose", raw_value="会议室扩容", source_quote="用途是会议室扩容"),
                SlotCandidate(slot_name="needed_by_date", raw_value="2026-09-20", source_quote="需要日期2026-09-20"),
                SlotCandidate(slot_name="currency", raw_value="人民币", source_quote="币种人民币"),
            ],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )
    result = merge_procurement_draft(
        fields=previous,
        pending={},
        sources={},
        validation=validation,
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["title"] == "会议室座椅"
    assert result.fields["purpose"] == "会议室扩容"
    assert result.fields["needed_by_date"] == "2026-09-20"
    assert result.fields["currency"] == "CNY"
    assert result.explicit_submit is False
    assert result.missing_fields == ()


def test_identity_only_reference_to_unique_complete_item_is_a_noop() -> None:
    existing_item = _complete_desk_item(specification=None)
    existing_source = {"source_turn_id": "turn-1"}

    result = merge_procurement_draft(
        fields={"items": [existing_item]},
        pending={},
        sources={"items": existing_source},
        validation=_item_name_only_validation("桌子"),
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["items"] == [existing_item]
    assert "items" not in result.pending
    assert result.sources["items"] == existing_source


def test_item_name_only_without_existing_complete_item_stays_partial() -> None:
    result = merge_procurement_draft(
        fields={},
        pending={},
        sources={},
        validation=_item_name_only_validation("桌子"),
        control=None,
        source_turn_id="turn-2",
    )

    assert "items" not in result.fields
    assert result.pending["items"]["reason_code"] == "item_fields_required"
    partial = result.pending["items"]["canonical_fragment"]["items"][0]
    assert partial["fields"] == {"item_name": "桌子"}


def test_identity_only_reference_to_duplicate_complete_items_is_not_dropped() -> None:
    first = _complete_desk_item(specification="红色")
    second = _complete_desk_item(specification="蓝色")

    result = merge_procurement_draft(
        fields={"items": [first, second]},
        pending={},
        sources={"items": {"source_turn_id": "turn-1"}},
        validation=_item_name_only_validation("桌子"),
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["items"] == [first, second]
    assert result.pending["items"]["reason_code"] == "item_fields_required"


def test_complete_draft_still_requires_explicit_submit_turn() -> None:
    fields = {
        "title": "会议室座椅",
        "purpose": "会议室扩容",
        "needed_by_date": "2026-09-20",
        "currency": "CNY",
        "items": [{
            "category_code": "office_supplies",
            "item_name": "椅子",
            "specification": None,
            "quantity": "3",
            "unit": "把",
            "estimated_unit_price": "500",
        }],
    }
    result = merge_procurement_draft(
        fields=fields,
        pending={},
        sources={},
        validation=CandidateValidationResult(accepted={}, pending={}, rejected=()),
        control=None,
        source_turn_id="turn-3",
        explicit_submit=True,
    )

    assert result.intent == "submit_request"
    assert result.explicit_submit is True
    assert result.missing_fields == ()


def test_procurement_date_conflict_does_not_overwrite_existing_value() -> None:
    validation = validate_procurement_candidates(
        text="需要日期改为2026-10-01",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="needed_by_date",
                raw_value="2026-10-01",
                source_quote="需要日期改为2026-10-01",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )

    result = merge_procurement_draft(
        fields={"needed_by_date": "2026-09-20"},
        pending={},
        sources={"needed_by_date": {"source_turn_id": "turn-1"}},
        validation=validation,
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["needed_by_date"] == "2026-09-20"
    assert result.pending["needed_by_date"]["reason_code"] == "draft_value_conflict"


def test_procurement_partial_date_stays_pending_until_complete_date_is_given() -> None:
    validation = validate_procurement_candidates(
        text="9.30需要",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="needed_by_date", raw_value="9.30", source_quote="9.30需要",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )

    result = merge_procurement_draft(
        fields={}, pending={}, sources={}, validation=validation,
        control=None, source_turn_id="turn-1",
    )

    assert "needed_by_date" not in result.fields
    assert result.pending["needed_by_date"]["reason_code"] == "date_year_required"
    assert "needed_by_date" in result.missing_fields


def test_partial_needed_date_converges_when_next_turn_only_supplies_year() -> None:
    first_validation = validate_procurement_candidates(
        text="需要日期9月10日",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="needed_by_date",
                raw_value="9月10日",
                source_quote="需要日期9月10日",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_procurement_draft(
        fields={},
        pending={},
        sources={},
        validation=first_validation,
        control=None,
        source_turn_id="turn-1",
    )

    second_validation = validate_procurement_candidates(
        text="就是2026年",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="needed_by_year",
                raw_value="就是2026年",
                source_quote="就是2026年",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )
    second = merge_procurement_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=second_validation,
        control=None,
        source_turn_id="turn-2",
        today=date(2026, 8, 28),
    )

    assert second.fields["needed_by_date"] == "2026-09-10"
    assert "needed_by_year" not in second.fields
    assert "needed_by_year" not in second.pending
    assert "needed_by_year" not in second.sources
    assert "needed_by_date" not in second.pending
    assert "needed_by_date" not in second.missing_fields
    date_source = cast(dict[str, object], second.sources["needed_by_date"])
    assert date_source["source_turn_id"] == "turn-2"
    supporting = cast(list[dict[str, object]], date_source["supporting_sources"])
    assert supporting[0]["source_turn_id"] == "turn-1"


def test_needed_year_without_a_pending_needed_date_is_not_persisted() -> None:
    validation = validate_procurement_candidates(
        text="2026年",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="needed_by_year",
                raw_value="2026年",
                source_quote="2026年",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )

    result = merge_procurement_draft(
        fields={},
        pending={},
        sources={},
        validation=validation,
        control=None,
        source_turn_id="turn-1",
    )

    assert "needed_by_year" not in result.fields
    assert "needed_by_year" not in result.pending
    assert "needed_by_year" not in result.sources
    assert "needed_by_date" in result.missing_fields


def test_invalid_year_date_combination_stays_pending_without_inventing_date() -> None:
    first_validation = validate_procurement_candidates(
        text="需要日期2月29日",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="needed_by_date",
                raw_value="2月29日",
                source_quote="需要日期2月29日",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_procurement_draft(
        fields={},
        pending={},
        sources={},
        validation=first_validation,
        control=None,
        source_turn_id="turn-1",
    )
    second_validation = validate_procurement_candidates(
        text="年份是2027",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="needed_by_year",
                raw_value="年份是2027",
                source_quote="年份是2027",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )

    second = merge_procurement_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=second_validation,
        control=None,
        source_turn_id="turn-2",
        today=date(2026, 8, 28),
    )

    assert "needed_by_date" not in second.fields
    assert second.pending["needed_by_date"]["reason_code"] == "date_invalid_for_year"
    assert second.pending["needed_by_date"]["canonical_fragment"] == {
        "month": 2,
        "day": 29,
        "provided_year": 2027,
    }
    assert "needed_by_date" in second.missing_fields


def test_partial_item_merges_across_turns_without_repeating_verified_leaves() -> None:
    first_validation = validate_procurement_candidates(
        text="办公用品桌子，单价600",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "桌子",
                    "estimated_unit_price": "600",
                    "unit": None,
                    "category_hint": "办公用品",
                },
                source_quote="办公用品桌子，单价600",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_procurement_draft(
        fields={},
        pending={},
        sources={},
        validation=first_validation,
        control=None,
        source_turn_id="turn-1",
    )

    assert "items" not in first.fields
    assert first.missing_fields == (
        "title",
        "purpose",
        "needed_by_date",
        "currency",
        "items[0].quantity",
        "items[0].unit",
    )

    second_validation = validate_procurement_candidates(
        text="数量一个，单位个",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "quantity": "一",
                    "unit": "个",
                    "category_hint": None,
                },
                source_quote="数量一个，单位个",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )
    second = merge_procurement_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=second_validation,
        control=None,
        source_turn_id="turn-2",
    )

    assert second.fields["items"] == [{
        "category_code": "office_supplies",
        "item_name": "桌子",
        "specification": None,
        "quantity": "1",
        "unit": "个",
        "estimated_unit_price": "600",
    }]
    assert "items" not in second.pending
    assert second.missing_fields == (
        "title",
        "purpose",
        "needed_by_date",
        "currency",
    )


def test_complete_item_without_specification_promotes_partial_and_keeps_source() -> None:
    first_text = "办公用品类红色桌子"
    first_validation = validate_procurement_candidates(
        text=first_text,
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "桌子",
                    "specification": "红色",
                    "unit": None,
                    "category_hint": "办公用品类",
                },
                source_quote=first_text,
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_procurement_draft(
        fields={},
        pending={},
        sources={},
        validation=first_validation,
        control=None,
        source_turn_id="turn-1",
    )

    result = merge_procurement_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=_complete_desk_validation(specification=None),
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["items"] == [_complete_desk_item(specification="红色")]
    assert "items" not in result.pending
    specification_source = result.sources["items"]["items"][0][
        "field_sources"
    ]["specification"]
    assert specification_source["source_turn_id"] == "turn-1"


def test_complete_item_without_specification_keeps_existing_complete_value() -> None:
    existing_source = {
        "source_turn_id": "turn-1",
        "source_kind": "user_explicit",
    }
    existing_item = _complete_desk_item(specification="红色")

    result = merge_procurement_draft(
        fields={"items": [existing_item]},
        pending={},
        sources={"items": existing_source},
        validation=_complete_desk_validation(specification=None),
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["items"] == [existing_item]
    assert "items" not in result.pending
    assert result.sources["items"] == existing_source


def test_legacy_complete_item_without_specification_key_matches_omission() -> None:
    existing_source = {
        "source_turn_id": "turn-1",
        "source_kind": "user_explicit",
    }
    existing_item = _complete_desk_item(specification=None)
    existing_item.pop("specification")

    result = merge_procurement_draft(
        fields={"items": [existing_item]},
        pending={},
        sources={"items": existing_source},
        validation=_complete_desk_validation(specification=None),
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["items"] == [existing_item]
    assert "specification" not in result.fields["items"][0]
    assert "items" not in result.pending
    assert result.sources["items"] == existing_source


def test_legacy_required_change_does_not_add_null_specification() -> None:
    existing_source = {"source_turn_id": "turn-1"}
    existing_item = _complete_desk_item(specification=None)
    existing_item.pop("specification")
    expected_candidate = dict(existing_item)
    expected_candidate["estimated_unit_price"] = "700"

    result = merge_procurement_draft(
        fields={"items": [existing_item]},
        pending={},
        sources={"items": existing_source},
        validation=_complete_desk_validation(specification=None, price="700"),
        control=None,
        source_turn_id="turn-2",
    )

    conflict = result.pending["items"]
    assert conflict["reason_code"] == "draft_value_conflict"
    assert conflict["candidate_value"] == [expected_candidate]
    assert "specification" not in conflict["candidate_value"][0]
    assert result.fields["items"] == [existing_item]
    assert result.sources["items"] == existing_source


def test_required_leaf_change_still_conflicts_without_clearing_specification() -> None:
    existing_source = {"source_turn_id": "turn-1"}
    existing_item = _complete_desk_item(specification="红色")

    result = merge_procurement_draft(
        fields={"items": [existing_item]},
        pending={},
        sources={"items": existing_source},
        validation=_complete_desk_validation(specification=None, price="700"),
        control=None,
        source_turn_id="turn-2",
    )

    conflict = result.pending["items"]
    assert conflict["reason_code"] == "draft_value_conflict"
    assert conflict["candidate_value"] == [
        _complete_desk_item(specification="红色", price="700")
    ]
    assert result.fields["items"] == [existing_item]
    assert result.sources["items"] == existing_source


def test_explicit_specification_change_still_conflicts() -> None:
    existing_source = {"source_turn_id": "turn-1"}
    existing_item = _complete_desk_item(specification="红色")

    result = merge_procurement_draft(
        fields={"items": [existing_item]},
        pending={},
        sources={"items": existing_source},
        validation=_complete_desk_validation(specification="蓝色"),
        control=None,
        source_turn_id="turn-2",
    )

    conflict = result.pending["items"]
    assert conflict["reason_code"] == "draft_value_conflict"
    assert conflict["candidate_value"] == [
        _complete_desk_item(specification="蓝色")
    ]
    assert result.fields["items"] == [existing_item]
    assert result.sources["items"] == existing_source


def test_omitted_specification_does_not_merge_duplicate_complete_identity() -> None:
    existing_source = {"source_turn_id": "turn-1"}
    first = _complete_desk_item(specification="红色")
    second = _complete_desk_item(specification="蓝色")

    result = merge_procurement_draft(
        fields={"items": [first, second]},
        pending={},
        sources={"items": existing_source},
        validation=_complete_desk_validation(specification=None),
        control=None,
        source_turn_id="turn-2",
    )

    conflict = result.pending["items"]
    assert conflict["reason_code"] == "draft_value_conflict"
    assert conflict["candidate_value"] == [
        _complete_desk_item(specification=None)
    ]
    assert result.fields["items"] == [first, second]
    assert result.sources["items"] == existing_source


def test_corrected_rejected_item_leaf_can_promote_the_verified_partial() -> None:
    first_validation = validate_procurement_candidates(
        text="办公用品桌子数量一，单位个，单价100000000000000",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "桌子",
                    "quantity": "一",
                    "unit": "个",
                    "estimated_unit_price": "100000000000000",
                    "category_hint": "办公用品",
                },
                source_quote="办公用品桌子数量一，单位个，单价100000000000000",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_procurement_draft(
        fields={}, pending={}, sources={}, validation=first_validation,
        control=None, source_turn_id="turn-1",
    )

    rejected = first.pending["items"]["canonical_fragment"]["items"][0][
        "rejected_fields"
    ]
    assert [item["field_path"] for item in rejected] == [
        "items[0].estimated_unit_price"
    ]

    correction = validate_procurement_candidates(
        text="单价600",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "estimated_unit_price": "600",
                    "unit": None,
                    "category_hint": None,
                },
                source_quote="单价600",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )
    result = merge_procurement_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=correction,
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["items"][0]["estimated_unit_price"] == "600"
    assert "items" not in result.pending


def test_correcting_one_rejected_item_leaf_preserves_other_rejections() -> None:
    first_validation = validate_procurement_candidates(
        text="办公用品桌子1000000000000个，单价100000000000000",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "桌子",
                    "quantity": "1000000000000个",
                    "unit": None,
                    "estimated_unit_price": "100000000000000",
                    "category_hint": "办公用品",
                },
                source_quote=(
                    "办公用品桌子1000000000000个，单价100000000000000"
                ),
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_procurement_draft(
        fields={}, pending={}, sources={}, validation=first_validation,
        control=None, source_turn_id="turn-1",
    )
    correction = validate_procurement_candidates(
        text="单价600",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "estimated_unit_price": "600",
                    "unit": None,
                    "category_hint": None,
                },
                source_quote="单价600",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )

    result = merge_procurement_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=correction,
        control=None,
        source_turn_id="turn-2",
    )

    item = result.pending["items"]["canonical_fragment"]["items"][0]
    assert item["fields"]["estimated_unit_price"] == "600"
    assert [entry["field_path"] for entry in item["rejected_fields"]] == [
        "items[0].quantity"
    ]
    assert "items" not in result.fields


def test_promoted_partial_item_projects_all_verified_leaf_sources() -> None:
    first_validation = validate_procurement_candidates(
        text="办公用品桌子，单价600",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "桌子",
                    "estimated_unit_price": "600",
                    "unit": None,
                    "category_hint": "办公用品",
                },
                source_quote="办公用品桌子，单价600",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_procurement_draft(
        fields={}, pending={}, sources={}, validation=first_validation,
        control=None, source_turn_id="turn-1",
    )
    completion = validate_procurement_candidates(
        text="数量一个，单位个",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "quantity": "一",
                    "unit": "个",
                    "category_hint": None,
                },
                source_quote="数量一个，单位个",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )

    result = merge_procurement_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=completion,
        control=None,
        source_turn_id="turn-2",
    )

    source_projection = result.sources["items"]["items"][0]
    assert set(source_projection["field_sources"]) == {
        "item_name",
        "quantity",
        "unit",
        "estimated_unit_price",
        "category_code",
    }
    assert source_projection["field_sources"]["item_name"][
        "source_turn_id"
    ] == "turn-1"
    assert source_projection["field_sources"]["estimated_unit_price"][
        "source_turn_id"
    ] == "turn-1"
    assert source_projection["field_sources"]["quantity"][
        "source_turn_id"
    ] == "turn-2"
    assert source_projection["field_sources"]["unit"][
        "source_turn_id"
    ] == "turn-2"


def test_mixed_complete_and_partial_items_keep_exact_leaf_missing_paths() -> None:
    validation = validate_procurement_candidates(
        text="办公用品椅子一把单价500；IT设备显示器两台",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[
                SlotCandidate(
                    slot_name="items",
                    raw_value={
                        "item_name": "椅子",
                        "quantity": "一",
                        "unit": "把",
                        "estimated_unit_price": "500",
                        "category_hint": "办公用品",
                    },
                    source_quote="办公用品椅子一把单价500",
                ),
                SlotCandidate(
                    slot_name="items",
                    raw_value={
                        "item_name": "显示器",
                        "quantity": "两",
                        "unit": "台",
                        "category_hint": "IT设备",
                    },
                    source_quote="IT设备显示器两台",
                ),
            ],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )

    result = merge_procurement_draft(
        fields={},
        pending={},
        sources={},
        validation=validation,
        control=None,
        source_turn_id="turn-1",
    )

    assert [item["item_name"] for item in result.fields["items"]] == ["椅子"]
    assert result.missing_fields[-1] == "items[1].estimated_unit_price"
    fragment = result.pending["items"]["canonical_fragment"]
    assert fragment["items"][0]["fields"]["item_name"] == "显示器"


def test_ambiguous_zero_leaf_item_blocker_survives_draft_merge() -> None:
    complete_quote = "办公用品椅子一把单价500元"
    repeated_quote = "办公用品桌子一个单价600元"
    text = f"{complete_quote}；{repeated_quote}；再次说明：{repeated_quote}"
    validation = validate_procurement_candidates(
        text=text,
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[
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
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )

    result = merge_procurement_draft(
        fields={},
        pending={},
        sources={},
        validation=validation,
        control=None,
        source_turn_id="turn-1",
        explicit_submit=True,
    )

    assert [item["item_name"] for item in result.fields["items"]] == ["椅子"]
    assert result.pending["items"]["reason_code"] == "source_quote_ambiguous"
    blocker = result.pending["items"]["canonical_fragment"]["items"][0]
    assert blocker["fields"] == {}
    assert result.missing_fields[-1] == "items[1]"
    assert result.explicit_submit is True


def test_unstructured_pending_item_disposition_is_fail_closed_by_draft_merge() -> None:
    complete = _complete_desk_validation(
        specification=None,
        source_turn_id="turn-1",
    )
    validation = CandidateValidationResult(
        accepted=complete.accepted,
        pending={
            "items": PendingCandidate(
                slot_name="items",
                reason_code="source_quote_ambiguous",
                canonical_fragment=None,
                provenance=None,
            ),
        },
        rejected=(),
    )

    result = merge_procurement_draft(
        fields={},
        pending={},
        sources={},
        validation=validation,
        control=None,
        source_turn_id="turn-1",
        explicit_submit=True,
    )

    assert result.pending["items"]["reason_code"] == "source_quote_ambiguous"
    assert result.pending["items"]["canonical_fragment"]["items"][0][
        "fields"
    ] == {}
    assert result.missing_fields[-1] == "items[1]"


def test_fully_rejected_zero_leaf_sibling_blocks_a_later_exact_submit() -> None:
    complete_quote = "办公用品椅子一把单价500元"
    rejected_quote = "另一个物品单价大概六百块"
    first_validation = validate_procurement_candidates(
        text=f"{complete_quote}；{rejected_quote}",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[
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
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_procurement_draft(
        fields={},
        pending={},
        sources={},
        validation=first_validation,
        control=None,
        source_turn_id="turn-1",
    )

    later_submit = merge_procurement_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=CandidateValidationResult(accepted={}, pending={}, rejected=()),
        control=None,
        source_turn_id="turn-2",
        explicit_submit=True,
    )

    assert later_submit.pending["items"]["canonical_fragment"]["items"][0][
        "fields"
    ] == {}
    assert later_submit.missing_fields[-1] == "items[1]"
    assert later_submit.explicit_submit is True


def test_multiple_pending_items_require_unambiguous_current_turn_identity() -> None:
    first_validation = validate_procurement_candidates(
        text="办公用品桌子单价600；办公用品椅子单价500",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[
                SlotCandidate(
                    slot_name="items",
                    raw_value={
                        "item_name": "桌子",
                        "estimated_unit_price": "600",
                        "unit": None,
                        "category_hint": "办公用品",
                    },
                    source_quote="办公用品桌子单价600",
                ),
                SlotCandidate(
                    slot_name="items",
                    raw_value={
                        "item_name": "椅子",
                        "estimated_unit_price": "500",
                        "unit": None,
                        "category_hint": "办公用品",
                    },
                    source_quote="办公用品椅子单价500",
                ),
            ],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_procurement_draft(
        fields={}, pending={}, sources={}, validation=first_validation,
        control=None, source_turn_id="turn-1",
    )
    follow_up = validate_procurement_candidates(
        text="数量一个，单位个",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "quantity": "一",
                    "unit": "个",
                    "category_hint": None,
                },
                source_quote="数量一个，单位个",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )

    result = merge_procurement_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=follow_up,
        control=None,
        source_turn_id="turn-2",
    )

    assert result.pending["items"]["reason_code"] == (
        "item_association_ambiguous"
    )
    existing = result.pending["items"]["canonical_fragment"]["items"]
    assert [item["fields"]["item_name"] for item in existing] == [
        "桌子",
        "椅子",
    ]
    assert "items" not in result.fields


def test_partial_item_leaf_conflict_preserves_verified_value() -> None:
    first_validation = validate_procurement_candidates(
        text="办公用品桌子，单价600",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "桌子",
                    "estimated_unit_price": "600",
                    "unit": None,
                    "category_hint": "办公用品",
                },
                source_quote="办公用品桌子，单价600",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_procurement_draft(
        fields={}, pending={}, sources={}, validation=first_validation,
        control=None, source_turn_id="turn-1",
    )
    conflicting = validate_procurement_candidates(
        text="桌子单价改成700",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "item_name": "桌子",
                    "estimated_unit_price": "700",
                    "unit": None,
                    "category_hint": None,
                },
                source_quote="桌子单价改成700",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )

    result = merge_procurement_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=conflicting,
        control=None,
        source_turn_id="turn-2",
    )

    assert result.pending["items"]["reason_code"] == "item_leaf_conflict"
    item = result.pending["items"]["canonical_fragment"]["items"][0]
    assert item["fields"]["estimated_unit_price"] == "600"
    assert item["conflicts"]["estimated_unit_price"]["candidate_value"] == "700"


def test_legacy_partial_item_fragment_is_upgraded_without_losing_item_name() -> None:
    follow_up = validate_procurement_candidates(
        text="办公用品，数量一个，单位个，单价600",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="items",
                raw_value={
                    "quantity": "一",
                    "unit": "个",
                    "estimated_unit_price": "600",
                    "category_hint": "办公用品",
                },
                source_quote="办公用品，数量一个，单位个，单价600",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )
    legacy_pending = {
        "items": {
            "slot_name": "items",
            "reason_code": "item_fields_required",
            "canonical_fragment": {
                "item_name": "桌子",
                "missing_fields": ["quantity", "unit", "estimated_unit_price"],
            },
            "provenance": {
                "source_turn_id": "turn-1",
                "source_kind": "user_explicit",
                "slot_schema_version": "slot-extraction-v1",
                "source_spans": [[0, 2]],
                "validator_version": "procurement-slot-validator-v2",
                "validation_status": "pending",
                "match_kind": "original_exact",
            },
        }
    }

    result = merge_procurement_draft(
        fields={},
        pending=legacy_pending,
        sources={},
        validation=follow_up,
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["items"][0]["item_name"] == "桌子"
    assert result.fields["items"][0]["quantity"] == "1"
    assert "items" not in result.pending


def test_procurement_request_id_does_not_require_submission_fields() -> None:
    request_id = uuid4()
    validation = validate_procurement_candidates(
        text=f"查看采购申请 {request_id}",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="request_id", raw_value=str(request_id), source_quote=str(request_id),
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )

    result = merge_procurement_draft(
        fields={}, pending={}, sources={}, validation=validation,
        control=None, source_turn_id="turn-1",
    )

    assert result.fields["request_id"] == str(request_id)
    assert result.missing_fields == ()


def test_procurement_clear_control_is_deterministic() -> None:
    control = parse_draft_control(
        "清空草稿",
        pending_conflict_names=(),
        field_labels={},
    )
    result = merge_procurement_draft(
        fields={"title": "原标题"},
        pending={},
        sources={},
        validation=CandidateValidationResult(accepted={}, pending={}, rejected=()),
        control=control,
        source_turn_id="turn-2",
    )

    assert result.clear_requested is True
    assert result.fields == {}


def test_explicit_submit_uses_only_canonical_seeded_draft_fields() -> None:
    fields = {
        "title": "会议室座椅",
        "purpose": "会议室扩容",
        "needed_by_date": "2026-09-20",
        "currency": "CNY",
        "items": [{
            "category_code": "office_supplies",
            "item_name": "椅子",
            "specification": None,
            "quantity": "3",
            "unit": "把",
            "estimated_unit_price": "500",
        }],
    }
    policy = build_procurement_tool_flow_policy(
        today_provider=lambda: date(2026, 8, 28),
        draft_fields=fields,
        draft_intent="submit_request",
    )
    registry = ToolRegistry(build_procurement_tool_definitions(
        cast(Session, None),
        procurement_runtime=object(),
        approval_runtime=object(),
    ))
    state = policy.start(
        "提交这份采购申请",
        ToolContext(actor_user_id=uuid4(), role=UserRole.EMPLOYEE),
        registry,
    )

    assert state.intent == "submit_request"
    calculate = registry.get("procurement.calculate_request_total")
    calculated_arguments = policy.normalize_arguments(state, calculate, {})
    assert calculated_arguments == {
        "items": [{
            "quantity": "3",
            "estimated_unit_price": "500",
        }]
    }
    assert CalculateRequestTotalInput.model_validate(calculated_arguments)
    policy.observe_read(
        state,
        calculate,
        calculated_arguments,
        {"currency": "CNY", "subtotals": ["1500.00"], "total": "1500.00"},
    )
    submit = registry.get("procurement.submit_request")
    assert policy.normalize_arguments(state, submit, {}) == fields


def test_seeded_procurement_draft_overrides_model_values() -> None:
    fields = {
        "title": "会议室座椅",
        "purpose": "会议室扩容",
        "needed_by_date": "2030-09-20",
        "currency": "CNY",
        "items": [{
            "category_code": "office_supplies",
            "item_name": "椅子",
            "specification": None,
            "quantity": "3",
            "unit": "把",
            "estimated_unit_price": "500",
        }],
    }
    policy = build_procurement_tool_flow_policy(
        today_provider=lambda: date(2026, 8, 28),
        draft_fields=fields,
        draft_intent="submit_request",
    )
    registry = ToolRegistry(build_procurement_tool_definitions(
        cast(Session, None),
        procurement_runtime=object(),
        approval_runtime=object(),
    ))
    state = policy.start(
        "提交这份采购申请",
        ToolContext(actor_user_id=uuid4(), role=UserRole.EMPLOYEE),
        registry,
    )

    normalized = policy.normalize_arguments(
        state,
        registry.get("procurement.submit_request"),
        {
            "title": "model placeholder",
            "purpose": "model placeholder",
            "needed_by_date": "2035-01-01",
            "currency": "USD",
            "items": [{
                "category_code": "other",
                "item_name": "model placeholder",
                "quantity": "999",
                "unit": "件",
                "estimated_unit_price": "999999",
            }],
        },
    )

    assert normalized == fields
