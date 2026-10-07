from __future__ import annotations

import importlib

import pytest

from policy_api.slot_extraction.errors import SlotExtractionError


def _module():
    return importlib.import_module("policy_api.slot_extraction.merge")


def _provenance(turn: str = "turn-2"):
    return _module().CandidateProvenance(
        source_turn_id=turn,
        source_kind="user_explicit",
        slot_schema_version="slot-extraction-v1",
        source_spans=((2, 8),),
        validator_version="validator-v1",
        validation_status="accepted",
        match_kind="original_exact",
    )


def _accepted(name: str, value: object):
    return _module().AcceptedCandidate(
        slot_name=name,
        canonical_value=value,
        provenance=_provenance(),
    )


def _control(action: str, field_name: str | None = None):
    controls = importlib.import_module("policy_api.slot_extraction.controls")
    return controls.DraftControl(
        version="draft-control-v1",
        action=controls.DraftControlAction(action),
        field_name=field_name,
        source_quote="用新的",
        source_span=(0, 3),
    )


def test_same_canonical_value_is_noop() -> None:
    result = _module().merge_scalar(
        field_name="leave_type_code",
        current="annual",
        candidate=_accepted("leave_type_code", "annual"),
        existing_source={"source_turn_id": "turn-1"},
    )

    assert result.value == "annual"
    assert result.changed is False
    assert result.pending is None
    assert result.source == {"source_turn_id": "turn-1"}


def test_different_value_becomes_pending_conflict_without_explicit_replace() -> None:
    result = _module().merge_scalar(
        field_name="needed_by_date",
        current="2026-09-30",
        candidate=_accepted("needed_by_date", "2026-10-01"),
        existing_source={"source_turn_id": "turn-1"},
    )

    assert result.value == "2026-09-30"
    assert result.changed is False
    assert result.pending["reason_code"] == "draft_value_conflict"
    assert result.pending["candidate_value"] == "2026-10-01"


def test_source_bound_explicit_replace_can_replace_one_field() -> None:
    result = _module().merge_scalar(
        field_name="needed_by_date",
        current="2026-09-30",
        candidate=_accepted("needed_by_date", "2026-10-01"),
        existing_source={"source_turn_id": "turn-1"},
        control=_control("confirm_replace", "needed_by_date"),
    )

    assert result.value == "2026-10-01"
    assert result.changed is True
    assert result.pending is None


def test_one_turn_cannot_create_and_confirm_its_own_conflict() -> None:
    with pytest.raises(
        SlotExtractionError,
        match="draft_conflict_confirmation_not_prior",
    ):
        _module().apply_pending_confirmation(
            preexisting=False,
            fields={"reason": "原原因"},
            sources={"reason": {"source_turn_id": "turn-1"}},
            pending={
                "reason": _module().pending_conflict(
                    field_name="reason",
                    current_value="原原因",
                    candidate=_accepted("reason", "新原因"),
                )
            },
            control=_control("confirm_replace", "reason"),
        )


def test_preexisting_single_conflict_can_be_confirmed_next_turn() -> None:
    result = _module().apply_pending_confirmation(
        preexisting=True,
        fields={"reason": "原原因"},
        sources={"reason": {"source_turn_id": "turn-1"}},
        pending={
            "reason": _module().pending_conflict(
                field_name="reason",
                current_value="原原因",
                candidate=_accepted("reason", "新原因"),
            )
        },
        control=_control("confirm_replace"),
    )

    assert result.fields["reason"] == "新原因"
    assert "reason" not in result.pending


def test_ambiguous_confirmation_never_replaces_multiple_conflicts() -> None:
    with pytest.raises(
        SlotExtractionError,
        match="draft_conflict_confirmation_ambiguous",
    ):
        _module().apply_pending_confirmation(
            preexisting=True,
            fields={"reason": "原原因", "start_date": "2026-09-01"},
            sources={},
            pending={
                "reason": _module().pending_conflict(
                    field_name="reason",
                    current_value="原原因",
                    candidate=_accepted("reason", "新原因"),
                ),
                "start_date": _module().pending_conflict(
                    field_name="start_date",
                    current_value="2026-09-01",
                    candidate=_accepted("start_date", "2026-09-02"),
                ),
            },
            control=_control("confirm_replace"),
        )


def test_keep_existing_drops_only_target_conflict() -> None:
    result = _module().apply_pending_confirmation(
        preexisting=True,
        fields={"reason": "原原因"},
        sources={"reason": {"source_turn_id": "turn-1"}},
        pending={
            "reason": _module().pending_conflict(
                field_name="reason",
                current_value="原原因",
                candidate=_accepted("reason", "新原因"),
            )
        },
        control=_control("keep_existing", "reason"),
    )

    assert result.fields["reason"] == "原原因"
    assert result.pending == {}
