from datetime import date
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from policy_api.hr.draft import merge_hr_draft
from policy_api.hr.slot_validation import validate_hr_candidates
from policy_api.hr.tool_flow_policy import build_hr_tool_flow_policy
from policy_api.hr.tools import build_hr_tool_definitions
from policy_api.models import UserRole
from policy_api.slot_extraction.controls import parse_draft_control
from policy_api.slot_extraction.merge import CandidateValidationResult
from policy_api.slot_extraction.schemas import SlotCandidate, SlotExtractionEnvelope
from policy_api.tools.definitions import ToolContext
from policy_api.tools.registry import ToolRegistry


def test_leave_draft_accepts_year_only_follow_up() -> None:
    first_validation = validate_hr_candidates(
        text="9.1-9.5，请假，请假类型调休 为了就医",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[
                SlotCandidate(
                    slot_name="date_range",
                    raw_value="9.1-9.5",
                    source_quote="9.1-9.5",
                ),
                SlotCandidate(
                    slot_name="leave_type_code",
                    raw_value="调休",
                    source_quote="请假类型调休",
                ),
                SlotCandidate(
                    slot_name="reason",
                    raw_value="就医",
                    source_quote="为了就医",
                ),
            ],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_hr_draft(
        fields={},
        pending={},
        sources={},
        validation=first_validation,
        control=None,
        source_turn_id="turn-1",
    )

    assert first.fields == {
        "leave_type_code": "compensatory",
        "reason": "就医",
    }
    assert first.pending["date_range"]["reason_code"] == "date_year_required"
    assert first.missing_fields == ("year",)

    second_validation = validate_hr_candidates(
        text="是，2026年",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="year",
                raw_value="2026",
                source_quote="2026年",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )
    second = merge_hr_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=second_validation,
        control=None,
        source_turn_id="turn-2",
    )

    assert second.fields["start_date"] == "2026-09-01"
    assert second.fields["end_date"] == "2026-09-05"
    assert "year" not in second.missing_fields
    assert second.sources["start_date"]["source_turn_id"] == "turn-2"
    assert "supporting_sources" in second.sources["start_date"]


def test_leave_draft_reuses_a_verified_year_for_a_later_partial_range() -> None:
    year_validation = validate_hr_candidates(
        text="年份是2026",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="year",
                raw_value="2026",
                source_quote="年份是2026",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_hr_draft(
        fields={},
        pending={},
        sources={},
        validation=year_validation,
        control=None,
        source_turn_id="turn-1",
    )
    range_validation = validate_hr_candidates(
        text="9月1号到9月3号",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="date_range",
                raw_value="9月1号到9月3号",
                source_quote="9月1号到9月3号",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )

    second = merge_hr_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=range_validation,
        control=None,
        source_turn_id="turn-2",
    )

    assert second.fields["year"] == 2026
    assert second.fields["start_date"] == "2026-09-01"
    assert second.fields["end_date"] == "2026-09-03"
    assert "date_range" not in second.pending


def test_conflicting_year_never_drives_pending_date_expansion() -> None:
    range_validation = validate_hr_candidates(
        text="9.1-9.3",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="date_range",
                raw_value="9.1-9.3",
                source_quote="9.1-9.3",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    pending_range = merge_hr_draft(
        fields={"year": 2026},
        pending={},
        sources={"year": {"source_turn_id": "turn-0"}},
        validation=range_validation,
        control=None,
        source_turn_id="turn-1",
    )
    conflicting_year = validate_hr_candidates(
        text="改成2027年",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="year",
                raw_value="2027",
                source_quote="2027年",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )

    result = merge_hr_draft(
        fields=pending_range.fields,
        pending=pending_range.pending,
        sources=pending_range.sources,
        validation=conflicting_year,
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["year"] == 2026
    assert result.fields["start_date"] == "2026-09-01"
    assert result.fields["end_date"] == "2026-09-03"
    assert result.pending["year"]["reason_code"] == "draft_value_conflict"


def test_confirmed_year_replacement_rebases_linked_dates() -> None:
    conflict_validation = validate_hr_candidates(
        text="年份改成2027",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="year",
                raw_value="2027",
                source_quote="2027",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )
    conflicted = merge_hr_draft(
        fields={
            "year": 2026,
            "start_date": "2026-09-01",
            "end_date": "2026-09-03",
        },
        pending={},
        sources={"year": {"source_turn_id": "turn-1"}},
        validation=conflict_validation,
        control=None,
        source_turn_id="turn-2",
    )
    control = parse_draft_control(
        "使用新值",
        pending_conflict_names=("year",),
        field_labels={"year": "请假年份"},
    )

    result = merge_hr_draft(
        fields=conflicted.fields,
        pending=conflicted.pending,
        sources=conflicted.sources,
        validation=CandidateValidationResult(accepted={}, pending={}, rejected=()),
        control=control,
        source_turn_id="turn-3",
    )

    assert result.fields["year"] == 2027
    assert result.fields["start_date"] == "2027-09-01"
    assert result.fields["end_date"] == "2027-09-03"
    assert result.pending == {}


def test_confirmed_year_replacement_keeps_cross_year_dates_ambiguous() -> None:
    conflict_validation = validate_hr_candidates(
        text="年份改成2027",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="year",
                raw_value="2027",
                source_quote="2027",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )
    conflicted = merge_hr_draft(
        fields={
            "year": 2026,
            "start_date": "2026-12-30",
            "end_date": "2027-01-02",
        },
        pending={},
        sources={"year": {"source_turn_id": "turn-1"}},
        validation=conflict_validation,
        control=None,
        source_turn_id="turn-2",
    )
    control = parse_draft_control(
        "使用新值",
        pending_conflict_names=("year",),
        field_labels={"year": "请假年份"},
    )

    result = merge_hr_draft(
        fields=conflicted.fields,
        pending=conflicted.pending,
        sources=conflicted.sources,
        validation=CandidateValidationResult(accepted={}, pending={}, rejected=()),
        control=control,
        source_turn_id="turn-3",
    )

    assert result.fields["year"] == 2027
    assert "start_date" not in result.fields
    assert "end_date" not in result.fields
    assert result.pending["date_range"]["reason_code"] == (
        "date_range_year_ambiguous"
    )


@pytest.mark.parametrize(
    ("slot_name", "raw_value", "pending_name", "missing_date_fields"),
    [
        (
            "date_range",
            "2027-09-01到2027-09-03",
            "date_range",
            {"start_date", "end_date"},
        ),
        (
            "start_date",
            "2027-09-01",
            "start_date",
            {"start_date"},
        ),
    ],
)
def test_full_date_from_a_different_year_reopens_structured_pending(
    slot_name: str,
    raw_value: str,
    pending_name: str,
    missing_date_fields: set[str],
) -> None:
    validation = validate_hr_candidates(
        text=raw_value,
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name=slot_name,
                raw_value=raw_value,
                source_quote=raw_value,
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )

    result = merge_hr_draft(
        fields={"year": 2026},
        pending={},
        sources={"year": {"source_turn_id": "turn-1"}},
        validation=validation,
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["year"] == 2026
    assert missing_date_fields.isdisjoint(result.fields)
    assert pending_name in result.pending
    assert missing_date_fields <= set(result.missing_fields)


def test_confirmed_year_replacement_reopens_invalid_leap_day() -> None:
    conflict_validation = validate_hr_candidates(
        text="年份改成2025年",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="year",
                raw_value="2025",
                source_quote="2025年",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )
    conflicted = merge_hr_draft(
        fields={
            "year": 2024,
            "start_date": "2024-02-29",
            "end_date": "2024-02-29",
        },
        pending={},
        sources={"year": {"source_turn_id": "turn-1"}},
        validation=conflict_validation,
        control=None,
        source_turn_id="turn-2",
    )
    control = parse_draft_control(
        "使用新值",
        pending_conflict_names=("year",),
        field_labels={"year": "请假年份"},
    )

    result = merge_hr_draft(
        fields=conflicted.fields,
        pending=conflicted.pending,
        sources=conflicted.sources,
        validation=CandidateValidationResult(accepted={}, pending={}, rejected=()),
        control=control,
        source_turn_id="turn-3",
    )

    assert result.fields["year"] == 2025
    assert "start_date" not in result.fields
    assert "end_date" not in result.fields
    assert result.pending["date_range"]["reason_code"] == "date_invalid_for_year"
    assert {"start_date", "end_date"} <= set(result.missing_fields)


def test_leave_draft_does_not_guess_year_without_user_value() -> None:
    validation = validate_hr_candidates(
        text="9月1日到9月5日请调休，原因是就医",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[
                SlotCandidate(
                    slot_name="date_range",
                    raw_value="9月1日到9月5日",
                    source_quote="9月1日到9月5日",
                ),
                SlotCandidate(
                    slot_name="leave_type_code",
                    raw_value="调休",
                    source_quote="调休",
                ),
                SlotCandidate(
                    slot_name="reason",
                    raw_value="就医",
                    source_quote="原因是就医",
                ),
            ],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    result = merge_hr_draft(
        fields={},
        pending={},
        sources={},
        validation=validation,
        control=None,
        source_turn_id="turn-1",
    )

    assert "start_date" not in result.fields
    assert "end_date" not in result.fields
    assert result.pending["date_range"]["canonical_fragment"]["start_month"] == 9


def test_leave_draft_clear_command_is_deterministic() -> None:
    control = parse_draft_control(
        "清空草稿",
        pending_conflict_names=(),
        field_labels={},
    )
    result = merge_hr_draft(
        fields={"leave_type_code": "annual"},
        pending={},
        sources={},
        validation=CandidateValidationResult(accepted={}, pending={}, rejected=()),
        control=control,
        source_turn_id="turn-2",
    )

    assert result.clear_requested is True
    assert result.fields == {}


def test_seeded_leave_draft_supplies_only_registered_tool_fields() -> None:
    fields = {
        "leave_type_code": "compensatory",
        "start_date": "2026-09-01",
        "end_date": "2026-09-05",
        "reason": "为了就医",
    }
    policy = build_hr_tool_flow_policy(
        today_provider=lambda: date(2026, 8, 28),
        draft_fields=fields,
        draft_intent="submit_leave",
    )
    registry = ToolRegistry(build_hr_tool_definitions(cast(Session, None)))
    state = policy.start(
        "是，2026年",
        ToolContext(actor_user_id=uuid4(), role=UserRole.EMPLOYEE),
        registry,
    )

    assert state.intent == "submit"
    duration = registry.get("hr.calculate_leave_duration")
    assert policy.normalize_arguments(state, duration, {}) == {
        "start_date": "2026-09-01",
        "end_date": "2026-09-05",
    }


def test_seeded_leave_draft_overrides_model_values() -> None:
    fields = {
        "leave_type_code": "compensatory",
        "start_date": "2026-09-01",
        "end_date": "2026-09-05",
        "reason": "为了就医",
    }
    policy = build_hr_tool_flow_policy(
        today_provider=lambda: date(2026, 8, 28),
        draft_fields=fields,
        draft_intent="submit_leave",
    )
    registry = ToolRegistry(build_hr_tool_definitions(cast(Session, None)))
    state = policy.start(
        "是，2026年",
        ToolContext(actor_user_id=uuid4(), role=UserRole.EMPLOYEE),
        registry,
    )

    normalized = policy.normalize_arguments(
        state,
        registry.get("hr.submit_leave_request"),
        {
            "leave_type_code": "annual",
            "start_date": "2035-01-01",
            "end_date": "2035-01-02",
            "reason": "model placeholder",
        },
    )

    assert normalized == fields


def test_leave_draft_expands_an_accepted_full_range() -> None:
    validation = validate_hr_candidates(
        text="2026-09-01到2026-09-05",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="date_range",
                raw_value="2026-09-01到2026-09-05",
                source_quote="2026-09-01到2026-09-05",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )

    result = merge_hr_draft(
        fields={},
        pending={},
        sources={},
        validation=validation,
        control=None,
        source_turn_id="turn-1",
    )

    assert result.fields == {
        "start_date": "2026-09-01",
        "end_date": "2026-09-05",
    }


def test_leave_draft_keeps_cross_year_partial_range_pending() -> None:
    first_validation = validate_hr_candidates(
        text="12.30-1.2",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="date_range",
                raw_value="12.30-1.2",
                source_quote="12.30-1.2",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )
    first = merge_hr_draft(
        fields={}, pending={}, sources={}, validation=first_validation,
        control=None, source_turn_id="turn-1",
    )
    year_validation = validate_hr_candidates(
        text="年份2026",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="year", raw_value="2026", source_quote="年份2026",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )

    second = merge_hr_draft(
        fields=first.fields,
        pending=first.pending,
        sources=first.sources,
        validation=year_validation,
        control=None,
        source_turn_id="turn-2",
    )

    assert "start_date" not in second.fields
    assert "end_date" not in second.fields
    assert second.pending["date_range"]["reason_code"] == (
        "date_range_year_ambiguous"
    )


def test_leave_draft_conflict_does_not_overwrite_canonical_value() -> None:
    validation = validate_hr_candidates(
        text="原因改为就医",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="reason", raw_value="就医", source_quote="原因改为就医",
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-2",
    )

    result = merge_hr_draft(
        fields={"reason": "探亲"},
        pending={},
        sources={"reason": {"source_turn_id": "turn-1"}},
        validation=validation,
        control=None,
        source_turn_id="turn-2",
    )

    assert result.fields["reason"] == "探亲"
    assert result.pending["reason"]["reason_code"] == "draft_value_conflict"


def test_request_id_draft_does_not_require_leave_submission_fields() -> None:
    request_id = uuid4()
    validation = validate_hr_candidates(
        text=f"查看申请 {request_id}",
        envelope=SlotExtractionEnvelope(
            schema_version="slot-extraction-v1",
            candidates=[SlotCandidate(
                slot_name="request_id",
                raw_value=str(request_id),
                source_quote=str(request_id),
            )],
        ),
        today=date(2026, 8, 28),
        source_turn_id="turn-1",
    )

    result = merge_hr_draft(
        fields={}, pending={}, sources={}, validation=validation,
        control=None, source_turn_id="turn-1",
    )

    assert result.fields["request_id"] == str(request_id)
    assert result.missing_fields == ()
