from __future__ import annotations

from datetime import date
import importlib
import uuid

import pytest

from policy_api.slot_extraction.schemas import SlotCandidate, SlotExtractionEnvelope


TODAY = date(2026, 8, 28)


def _module():
    return importlib.import_module("policy_api.hr.slot_validation")


def _candidate(name: str, value: str, quote: str) -> SlotCandidate:
    return SlotCandidate(slot_name=name, raw_value=value, source_quote=quote)


def _envelope(*candidates: SlotCandidate) -> SlotExtractionEnvelope:
    return SlotExtractionEnvelope(
        schema_version="slot-extraction-v1",
        candidates=list(candidates),
    )


def test_reason_from_real_user_phrase_is_accepted_with_source_span() -> None:
    result = _module().validate_hr_candidates(
        text="我想申请9.1-9.6年假，用于探亲",
        envelope=_envelope(_candidate("reason", "探亲", "用于探亲")),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert result.accepted["reason"].canonical_value == "探亲"
    provenance = result.accepted["reason"].provenance
    assert provenance.match_kind == "original_exact"
    assert provenance.source_spans == ((14, 18),)


def test_fullwidth_date_uses_controlled_normalization_and_original_span() -> None:
    result = _module().validate_hr_candidates(
        text="开始日期为２０２６．９．３０",
        envelope=_envelope(
            _candidate("start_date", "2026.9.30", "开始日期为2026.9.30")
        ),
        today=TODAY,
        source_turn_id="turn-1",
    )

    accepted = result.accepted["start_date"]
    assert accepted.canonical_value == "2026-09-30"
    assert accepted.provenance.match_kind == "controlled_normalized_exact"
    assert accepted.provenance.source_spans == ((0, 14),)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-9-3", "2026-09-03"),
        ("2026.9.3", "2026-09-03"),
        ("2026/9/3", "2026-09-03"),
        ("2026年9月3日", "2026-09-03"),
        ("明天", "2026-08-29"),
    ],
)
def test_supported_date_forms_canonicalize_deterministically(
    raw: str,
    expected: str,
) -> None:
    result = _module().validate_hr_candidates(
        text=f"开始日期{raw}",
        envelope=_envelope(_candidate("start_date", raw, f"开始日期{raw}")),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert result.accepted["start_date"].canonical_value == expected


def test_missing_year_range_is_pending_not_guessed() -> None:
    result = _module().validate_hr_candidates(
        text="9.1-9.6年假",
        envelope=_envelope(_candidate("date_range", "9.1-9.6", "9.1-9.6")),
        today=TODAY,
        source_turn_id="turn-1",
    )

    pending = result.pending["date_range"]
    assert pending.reason_code == "date_year_required"
    assert pending.canonical_fragment == {
        "start_month": 9,
        "start_day": 1,
        "end_month": 9,
        "end_day": 6,
    }


def test_impossible_and_reversed_dates_are_rejected() -> None:
    impossible = _module().validate_hr_candidates(
        text="开始日期2026-02-30",
        envelope=_envelope(
            _candidate("start_date", "2026-02-30", "开始日期2026-02-30")
        ),
        today=TODAY,
        source_turn_id="turn-1",
    )
    reversed_range = _module().validate_hr_candidates(
        text="2026-09-06到2026-09-01",
        envelope=_envelope(
            _candidate(
                "date_range",
                "2026-09-06到2026-09-01",
                "2026-09-06到2026-09-01",
            )
        ),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert impossible.rejected[0].reason_code == "date_invalid"
    assert reversed_range.rejected[0].reason_code == "date_range_reversed"


def test_multiple_distinct_leave_types_are_pending_ambiguous() -> None:
    result = _module().validate_hr_candidates(
        text="年假还是调休",
        envelope=_envelope(
            _candidate("leave_type_code", "年假", "年假"),
            _candidate("leave_type_code", "调休", "调休"),
        ),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert "leave_type_code" not in result.accepted
    assert result.pending["leave_type_code"].reason_code == "slot_value_ambiguous"


def test_uuid_and_year_are_canonical_only_when_explicit() -> None:
    request_id = uuid.uuid4()
    text = f"撤销申请 {request_id}，年份2026"
    result = _module().validate_hr_candidates(
        text=text,
        envelope=_envelope(
            _candidate("request_id", f" {request_id} ", str(request_id)),
            _candidate("year", "2026", "年份2026"),
        ),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert result.accepted["request_id"].canonical_value == str(request_id)
    assert result.accepted["year"].canonical_value == 2026


@pytest.mark.parametrize(
    "phrase",
    ["2026年", "年份2026", "年份是2026", "就是2026年"],
)
def test_natural_year_wrappers_canonicalize_after_source_binding(
    phrase: str,
) -> None:
    result = _module().validate_hr_candidates(
        text=phrase,
        envelope=_envelope(_candidate("year", phrase, phrase)),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert result.rejected == ()
    assert result.accepted["year"].canonical_value == 2026


@pytest.mark.parametrize(
    "phrase",
    ["26年", "明年", "大概2026年", "2026或2027"],
)
def test_noncanonical_or_ambiguous_year_phrases_are_not_accepted(
    phrase: str,
) -> None:
    result = _module().validate_hr_candidates(
        text=phrase,
        envelope=_envelope(_candidate("year", phrase, phrase)),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert "year" not in result.accepted


@pytest.mark.parametrize(
    "source_quote",
    [
        "大概2026年",
        "约2026年",
        "2026年左右",
        "2026或2027",
    ],
)
def test_stripped_year_token_is_rejected_in_ambiguous_source_context(
    source_quote: str,
) -> None:
    result = _module().validate_hr_candidates(
        text=source_quote,
        envelope=_envelope(_candidate("year", "2026", source_quote)),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert "year" not in result.accepted
    assert result.rejected == (
        _module().RejectedCandidate("year", "year_invalid"),
    )


@pytest.mark.parametrize(
    "source_quote",
    ["2026年", "年份2026", "年份是2026", "就是2026年"],
)
def test_stripped_year_token_remains_valid_in_exact_labelled_context(
    source_quote: str,
) -> None:
    result = _module().validate_hr_candidates(
        text=source_quote,
        envelope=_envelope(_candidate("year", "2026", source_quote)),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert result.rejected == ()
    assert result.accepted["year"].canonical_value == 2026


def test_year_source_binding_still_precedes_context_validation() -> None:
    result = _module().validate_hr_candidates(
        text="大概2027年",
        envelope=_envelope(_candidate("year", "2026", "大概2027年")),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert result.rejected == (
        _module().RejectedCandidate("year", "raw_value_not_in_source_quote"),
    )


@pytest.mark.parametrize(
    ("slot_name", "raw", "expected"),
    [
        ("start_date", "2026年9月3号", "2026-09-03"),
        ("start_date", "9月3号", None),
        (
            "date_range",
            "9月1号到9月3号",
            {
                "start_month": 9,
                "start_day": 1,
                "end_month": 9,
                "end_day": 3,
            },
        ),
    ],
)
def test_common_hao_date_suffixes_are_bounded_and_deterministic(
    slot_name: str,
    raw: str,
    expected: object,
) -> None:
    result = _module().validate_hr_candidates(
        text=raw,
        envelope=_envelope(_candidate(slot_name, raw, raw)),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert result.rejected == ()
    if expected is None:
        assert result.pending[slot_name].reason_code == "date_year_required"
        assert result.pending[slot_name].canonical_fragment == {
            "month": 9,
            "day": 3,
        }
    elif slot_name == "date_range":
        assert result.pending[slot_name].canonical_fragment == expected
    else:
        assert result.accepted[slot_name].canonical_value == expected


def test_overlong_reason_and_source_mismatch_are_rejected_independently() -> None:
    long_reason = "由" * 501
    result = _module().validate_hr_candidates(
        text=f"原因是{long_reason}",
        envelope=_envelope(
            _candidate("reason", long_reason, f"原因是{long_reason}"),
            _candidate("leave_type_code", "年假", "助手之前说年假"),
        ),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert {item.reason_code for item in result.rejected} == {
        "reason_too_long",
        "source_quote_not_found",
    }
    assert result.accepted == {}


def test_ambiguous_relative_date_is_pending() -> None:
    result = _module().validate_hr_candidates(
        text="下个月开始请假",
        envelope=_envelope(_candidate("start_date", "下个月", "下个月")),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert result.pending["start_date"].reason_code == "date_ambiguous"


def test_raw_value_must_be_traceable_inside_the_current_quote() -> None:
    result = _module().validate_hr_candidates(
        text="用于旅游，年假",
        envelope=_envelope(_candidate("reason", "探亲", "用于旅游")),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert result.accepted == {}
    assert result.rejected[0].reason_code == "raw_value_not_in_source_quote"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("年假", "annual"),
        ("annual_leave", "annual"),
        ("补休", "compensatory"),
        ("comp_time", "compensatory"),
    ],
)
def test_leave_type_aliases_are_closed_and_canonical(
    raw: str,
    expected: str,
) -> None:
    result = _module().validate_hr_candidates(
        text=f"请假类型{raw}",
        envelope=_envelope(_candidate("leave_type_code", raw, f"请假类型{raw}")),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert result.accepted["leave_type_code"].canonical_value == expected


def test_full_date_range_is_accepted_without_swapping() -> None:
    raw = "2026-09-01到2026-09-06"
    result = _module().validate_hr_candidates(
        text=f"请假时间{raw}",
        envelope=_envelope(_candidate("date_range", raw, raw)),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert result.accepted["date_range"].canonical_value == {
        "start_date": "2026-09-01",
        "end_date": "2026-09-06",
    }


def test_invalid_uuid_and_year_are_rejected() -> None:
    result = _module().validate_hr_candidates(
        text="申请 not-a-uuid，年份1999",
        envelope=_envelope(
            _candidate("request_id", "not-a-uuid", "not-a-uuid"),
            _candidate("year", "1999", "年份1999"),
        ),
        today=TODAY,
        source_turn_id="turn-1",
    )

    assert {item.reason_code for item in result.rejected} == {
        "request_id_invalid",
        "year_out_of_range",
    }


def test_hr_provider_schema_defines_leave_alias_and_date_precedence() -> None:
    schema_module = importlib.import_module("policy_api.hr.slot_schema")
    variants = schema_module.HR_SLOT_SCHEMA.provider_json["properties"][
        "candidates"
    ]["items"]["oneOf"]
    descriptions = {
        variant["properties"]["slot_name"]["const"]: variant["description"]
        for variant in variants
    }

    assert "年假" in descriptions["leave_type_code"]
    assert "comp_time" in descriptions["leave_type_code"]
    assert "only date_range" in descriptions["date_range"]
    assert "not part of one contiguous date range" in descriptions["start_date"]
    assert "follow-up" in descriptions["reason"]
    assert "开始日期" in descriptions["start_date"]
    assert "结束日期" in descriptions["end_date"]
    assert "must not emit date_range" in descriptions["date_range"]
    assert "exclude the introducer" in descriptions["reason"]
