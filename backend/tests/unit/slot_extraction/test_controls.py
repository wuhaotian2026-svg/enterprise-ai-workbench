from __future__ import annotations

import importlib

import pytest

from policy_api.slot_extraction.errors import SlotExtractionError


def _module():
    return importlib.import_module("policy_api.slot_extraction.controls")


def test_clear_control_is_closed_and_source_bound() -> None:
    control = _module().parse_draft_control(
        "清空草稿",
        pending_conflict_names=(),
        field_labels={},
    )

    assert control.action.value == "clear_draft"
    assert control.field_name is None
    assert control.source_quote == "清空草稿"
    assert control.source_span == (0, 4)


def test_single_preexisting_conflict_allows_unqualified_new_value_confirmation() -> None:
    control = _module().parse_draft_control(
        "用新的",
        pending_conflict_names=("reason",),
        field_labels={"reason": "原因"},
    )

    assert control.action.value == "confirm_replace"
    assert control.field_name == "reason"


def test_multiple_conflicts_reject_ambiguous_bulk_confirmation() -> None:
    control = _module().parse_draft_control(
        "都用新的",
        pending_conflict_names=("reason", "start_date"),
        field_labels={"reason": "原因", "start_date": "开始日期"},
    )

    assert control is None


def test_multiple_conflicts_require_one_exact_field_label() -> None:
    control = _module().parse_draft_control(
        "原因用新的",
        pending_conflict_names=("reason", "start_date"),
        field_labels={"reason": "原因", "start_date": "开始日期"},
    )

    assert control.action.value == "confirm_replace"
    assert control.field_name == "reason"


def test_keep_existing_targets_only_a_preexisting_conflict() -> None:
    control = _module().parse_draft_control(
        "保留原原因",
        pending_conflict_names=("reason", "start_date"),
        field_labels={"reason": "原因", "start_date": "开始日期"},
    )

    assert control.action.value == "keep_existing"
    assert control.field_name == "reason"


def test_business_sentence_is_not_reparsed_as_a_control() -> None:
    assert (
        _module().parse_draft_control(
            "把采购数量改成三把椅子",
            pending_conflict_names=("items",),
            field_labels={"items": "采购明细"},
        )
        is None
    )


@pytest.mark.parametrize("text", ["   ", "x" * 2001])
def test_current_turn_preflight_rejects_blank_or_oversized_text(text: str) -> None:
    with pytest.raises(SlotExtractionError, match="slot_extraction_request_invalid"):
        _module().validate_current_user_turn_text(text)
