from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import json
import re
from typing import Literal
from uuid import UUID

from policy_api.hr.slot_schema import HR_SLOT_SCHEMA
from policy_api.slot_extraction.errors import (
    SourceQuoteAmbiguous,
    SourceQuoteNotFound,
)
from policy_api.slot_extraction.merge import (
    AcceptedCandidate,
    CandidateProvenance,
    CandidateValidationResult,
    PendingCandidate,
    RejectedCandidate,
)
from policy_api.slot_extraction.normalization import (
    SourceQuoteMatch,
    locate_source_quote,
    normalize_with_spans,
)
from policy_api.slot_extraction.schemas import (
    SlotCandidate,
    SlotExtractionEnvelope,
    validate_envelope_for_module,
)


HR_SLOT_VALIDATOR_VERSION = "hr-slot-validator-v1"
_MIN_YEAR = 2000
_MAX_YEAR = 2100
_MAX_REASON_LENGTH = 500

_LEAVE_TYPE_ALIASES = {
    "annual": "annual",
    "annual_leave": "annual",
    "年假": "annual",
    "compensatory": "compensatory",
    "comp_time": "compensatory",
    "调休": "compensatory",
    "补休": "compensatory",
}
_AMBIGUOUS_DATE_WORDS = frozenset(
    {"下个月", "月底", "月底前", "尽快", "近期", "下周", "下星期"}
)
_RELATIVE_DATES = {"今天": 0, "明天": 1, "后天": 2}
_FULL_DATE = re.compile(r"^(?P<y>\d{4})[-./](?P<m>\d{1,2})[-./](?P<d>\d{1,2})$")
_FULL_CN_DATE = re.compile(
    r"^(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})(?:日|号)?$"
)
_PARTIAL_DATE = re.compile(
    r"^(?P<m>\d{1,2})(?:[.]|月)(?P<d>\d{1,2})(?:日|号)?$"
)
_FULL_RANGE = re.compile(
    r"^(?P<start>\d{4}[-./]\d{1,2}[-./]\d{1,2})\s*"
    r"(?:到|至|~|—|–|-)\s*"
    r"(?P<end>\d{4}[-./]\d{1,2}[-./]\d{1,2})$"
)
_FULL_CN_RANGE = re.compile(
    r"^(?P<sy>\d{4})年(?P<sm>\d{1,2})月(?P<sd>\d{1,2})(?:日|号)?\s*"
    r"(?:到|至|~|—|–|-)\s*"
    r"(?:(?P<ey>\d{4})年)?(?P<em>\d{1,2})月(?P<ed>\d{1,2})(?:日|号)?$"
)
_PARTIAL_RANGE = re.compile(
    r"^(?P<sm>\d{1,2})(?:[.]|月)(?P<sd>\d{1,2})(?:日|号)?\s*"
    r"(?:到|至|~|—|–|-)\s*"
    r"(?P<em>\d{1,2})(?:[.]|月)(?P<ed>\d{1,2})(?:日|号)?$"
)
_YEAR_VALUE = re.compile(
    r"^(?:(?:年份(?:是)?)|(?:就是))?(?P<year>\d{4})年?$"
)
_YEAR_TOKEN = re.compile(r"(?<!\d)\d{4}(?!\d)")
_YEAR_APPROXIMATE_PREFIX = re.compile(
    r"(?:大概|大约|约|差不多|预计|估计|大致|可能(?:是)?)\s*"
    r"(?:年份?\s*(?:是)?)?\s*$"
)
_YEAR_APPROXIMATE_SUFFIX = re.compile(r"^\s*年?\s*(?:左右|前后|上下)")

HrCandidateValidationResult = CandidateValidationResult


def _provenance(
    *,
    source_turn_id: str,
    match: SourceQuoteMatch,
    status: Literal["accepted", "pending"],
) -> CandidateProvenance:
    return CandidateProvenance(
        source_turn_id=source_turn_id,
        source_kind="user_explicit",
        slot_schema_version=HR_SLOT_SCHEMA.version,
        source_spans=((match.source_span.start, match.source_span.end),),
        validator_version=HR_SLOT_VALIDATOR_VERSION,
        validation_status=status,
        match_kind=match.match_kind,
    )


def _pending_provenance(
    provenance: CandidateProvenance,
) -> CandidateProvenance:
    return CandidateProvenance(
        source_turn_id=provenance.source_turn_id,
        source_kind=provenance.source_kind,
        slot_schema_version=provenance.slot_schema_version,
        source_spans=provenance.source_spans,
        validator_version=provenance.validator_version,
        validation_status="pending",
        match_kind=provenance.match_kind,
    )


def _raw_value_is_traceable(raw_value: str, original_quote: str) -> bool:
    normalized_raw = normalize_with_spans(raw_value.strip()).text
    normalized_quote = normalize_with_spans(original_quote).text
    return bool(normalized_raw) and normalized_raw in normalized_quote


def _date_value(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month_day_is_valid(month: int, day: int) -> bool:
    return _date_value(2024, month, day) is not None


def _canonical_year(raw_value: str) -> int | None:
    normalized = normalize_with_spans(raw_value.strip()).text
    match = _YEAR_VALUE.fullmatch(normalized)
    if match is None:
        return None
    return int(match.group("year"))


def _year_source_context_is_ambiguous(
    raw_value: str,
    original_quote: str,
) -> bool:
    canonical_year = _canonical_year(raw_value)
    if canonical_year is None:
        return False
    normalized_quote = normalize_with_spans(original_quote).text
    distinct_years = {match.group(0) for match in _YEAR_TOKEN.finditer(normalized_quote)}
    if len(distinct_years) > 1:
        return True
    token = str(canonical_year)
    for match in re.finditer(re.escape(token), normalized_quote):
        prefix = normalized_quote[:match.start()]
        suffix = normalized_quote[match.end():]
        if _YEAR_APPROXIMATE_PREFIX.search(prefix):
            return True
        if _YEAR_APPROXIMATE_SUFFIX.search(suffix):
            return True
    return False


def _parse_full_date(raw_value: str) -> date | None:
    normalized = normalize_with_spans(raw_value.strip()).text
    match = _FULL_DATE.fullmatch(normalized) or _FULL_CN_DATE.fullmatch(normalized)
    if match is None:
        return None
    return _date_value(
        int(match.group("y")),
        int(match.group("m")),
        int(match.group("d")),
    )


def _validate_date_candidate(
    *,
    candidate: SlotCandidate,
    raw_value: str,
    today: date,
    accepted_provenance: CandidateProvenance,
) -> AcceptedCandidate | PendingCandidate | RejectedCandidate:
    raw = normalize_with_spans(raw_value.strip()).text
    if raw in _RELATIVE_DATES:
        return AcceptedCandidate(
            slot_name=candidate.slot_name,
            canonical_value=(today + timedelta(days=_RELATIVE_DATES[raw])).isoformat(),
            provenance=accepted_provenance,
        )
    if raw in _AMBIGUOUS_DATE_WORDS:
        return PendingCandidate(
            slot_name=candidate.slot_name,
            reason_code="date_ambiguous",
            canonical_fragment=None,
            provenance=_pending_provenance(accepted_provenance),
        )

    full_match = _FULL_DATE.fullmatch(raw) or _FULL_CN_DATE.fullmatch(raw)
    if full_match is not None:
        value = _date_value(
            int(full_match.group("y")),
            int(full_match.group("m")),
            int(full_match.group("d")),
        )
        if value is None:
            return RejectedCandidate(candidate.slot_name, "date_invalid")
        return AcceptedCandidate(
            candidate.slot_name,
            value.isoformat(),
            accepted_provenance,
        )

    partial = _PARTIAL_DATE.fullmatch(raw)
    if partial is not None:
        month = int(partial.group("m"))
        day = int(partial.group("d"))
        if not _month_day_is_valid(month, day):
            return RejectedCandidate(candidate.slot_name, "date_invalid")
        return PendingCandidate(
            slot_name=candidate.slot_name,
            reason_code="date_year_required",
            canonical_fragment={"month": month, "day": day},
            provenance=_pending_provenance(accepted_provenance),
        )
    return RejectedCandidate(candidate.slot_name, "date_invalid")


def _validate_range_candidate(
    *,
    candidate: SlotCandidate,
    raw_value: str,
    accepted_provenance: CandidateProvenance,
) -> AcceptedCandidate | PendingCandidate | RejectedCandidate:
    raw = normalize_with_spans(raw_value.strip()).text
    full = _FULL_RANGE.fullmatch(raw)
    if full is not None:
        start = _parse_full_date(full.group("start"))
        end = _parse_full_date(full.group("end"))
        if start is None or end is None:
            return RejectedCandidate(candidate.slot_name, "date_invalid")
        if end < start:
            return RejectedCandidate(candidate.slot_name, "date_range_reversed")
        return AcceptedCandidate(
            candidate.slot_name,
            {"start_date": start.isoformat(), "end_date": end.isoformat()},
            accepted_provenance,
        )

    full_cn = _FULL_CN_RANGE.fullmatch(raw)
    if full_cn is not None:
        start_year = int(full_cn.group("sy"))
        end_year = int(full_cn.group("ey") or start_year)
        start = _date_value(
            start_year,
            int(full_cn.group("sm")),
            int(full_cn.group("sd")),
        )
        end = _date_value(
            end_year,
            int(full_cn.group("em")),
            int(full_cn.group("ed")),
        )
        if start is None or end is None:
            return RejectedCandidate(candidate.slot_name, "date_invalid")
        if end < start:
            return RejectedCandidate(candidate.slot_name, "date_range_reversed")
        return AcceptedCandidate(
            candidate.slot_name,
            {"start_date": start.isoformat(), "end_date": end.isoformat()},
            accepted_provenance,
        )

    partial = _PARTIAL_RANGE.fullmatch(raw)
    if partial is not None:
        fragment = {
            "start_month": int(partial.group("sm")),
            "start_day": int(partial.group("sd")),
            "end_month": int(partial.group("em")),
            "end_day": int(partial.group("ed")),
        }
        if not _month_day_is_valid(
            fragment["start_month"], fragment["start_day"]
        ) or not _month_day_is_valid(fragment["end_month"], fragment["end_day"]):
            return RejectedCandidate(candidate.slot_name, "date_invalid")
        return PendingCandidate(
            slot_name=candidate.slot_name,
            reason_code="date_year_required",
            canonical_fragment=fragment,
            provenance=_pending_provenance(accepted_provenance),
        )
    return RejectedCandidate(candidate.slot_name, "date_invalid")


def _classify_candidate(
    *,
    candidate: SlotCandidate,
    raw_value: str,
    today: date,
    provenance: CandidateProvenance,
) -> AcceptedCandidate | PendingCandidate | RejectedCandidate:
    slot_name = candidate.slot_name
    raw = raw_value.strip()
    if slot_name == "leave_type_code":
        canonical = _LEAVE_TYPE_ALIASES.get(raw.lower())
        if canonical is None:
            return RejectedCandidate(slot_name, "leave_type_invalid")
        return AcceptedCandidate(slot_name, canonical, provenance)
    if slot_name in {"start_date", "end_date"}:
        return _validate_date_candidate(
            candidate=candidate,
            raw_value=raw,
            today=today,
            accepted_provenance=provenance,
        )
    if slot_name == "date_range":
        return _validate_range_candidate(
            candidate=candidate,
            raw_value=raw,
            accepted_provenance=provenance,
        )
    if slot_name == "reason":
        if not raw:
            return RejectedCandidate(slot_name, "reason_empty")
        if len(raw) > _MAX_REASON_LENGTH:
            return RejectedCandidate(slot_name, "reason_too_long")
        return AcceptedCandidate(slot_name, raw, provenance)
    if slot_name == "request_id":
        try:
            canonical = str(UUID(raw))
        except ValueError:
            return RejectedCandidate(slot_name, "request_id_invalid")
        return AcceptedCandidate(slot_name, canonical, provenance)
    if slot_name == "year":
        canonical_year = _canonical_year(raw)
        if canonical_year is None:
            return RejectedCandidate(slot_name, "year_invalid")
        if canonical_year < _MIN_YEAR or canonical_year > _MAX_YEAR:
            return RejectedCandidate(slot_name, "year_out_of_range")
        return AcceptedCandidate(slot_name, canonical_year, provenance)
    return RejectedCandidate(slot_name, "slot_not_supported")


def _canonical_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def validate_hr_candidates(
    *,
    text: str,
    envelope: SlotExtractionEnvelope,
    today: date,
    source_turn_id: str,
) -> HrCandidateValidationResult:
    validate_envelope_for_module(envelope, HR_SLOT_SCHEMA)
    accepted_groups: dict[str, list[AcceptedCandidate]] = defaultdict(list)
    pending_groups: dict[str, list[PendingCandidate]] = defaultdict(list)
    rejected: list[RejectedCandidate] = []

    for candidate in envelope.candidates:
        raw_value = candidate.raw_value
        if not isinstance(raw_value, str):
            rejected.append(RejectedCandidate(candidate.slot_name, "raw_value_invalid"))
            continue
        try:
            match = locate_source_quote(text, candidate.source_quote)
        except SourceQuoteNotFound:
            rejected.append(
                RejectedCandidate(candidate.slot_name, "source_quote_not_found")
            )
            continue
        except SourceQuoteAmbiguous:
            pending_groups[candidate.slot_name].append(
                PendingCandidate(
                    slot_name=candidate.slot_name,
                    reason_code="source_quote_ambiguous",
                    canonical_fragment=None,
                    provenance=None,
                )
            )
            continue

        original_quote = text[match.source_span.start : match.source_span.end]
        if not _raw_value_is_traceable(raw_value, original_quote):
            rejected.append(
                RejectedCandidate(
                    candidate.slot_name,
                    "raw_value_not_in_source_quote",
                )
            )
            continue
        if (
            candidate.slot_name == "year"
            and _year_source_context_is_ambiguous(raw_value, original_quote)
        ):
            rejected.append(RejectedCandidate("year", "year_invalid"))
            continue
        classified = _classify_candidate(
            candidate=candidate,
            raw_value=raw_value,
            today=today,
            provenance=_provenance(
                source_turn_id=source_turn_id,
                match=match,
                status="accepted",
            ),
        )
        if isinstance(classified, AcceptedCandidate):
            accepted_groups[candidate.slot_name].append(classified)
        elif isinstance(classified, PendingCandidate):
            pending_groups[candidate.slot_name].append(classified)
        else:
            rejected.append(classified)

    accepted: dict[str, AcceptedCandidate] = {}
    pending: dict[str, PendingCandidate] = {}
    for slot_name in set(accepted_groups) | set(pending_groups):
        accepted_candidates = accepted_groups.get(slot_name, [])
        pending_candidates = pending_groups.get(slot_name, [])
        canonical_values = {
            _canonical_key(candidate.canonical_value)
            for candidate in accepted_candidates
        }
        if len(canonical_values) > 1 or (
            accepted_candidates and pending_candidates
        ) or len(pending_candidates) > 1:
            source = (
                _pending_provenance(accepted_candidates[0].provenance)
                if accepted_candidates
                else pending_candidates[0].provenance
            )
            pending[slot_name] = PendingCandidate(
                slot_name=slot_name,
                reason_code="slot_value_ambiguous",
                canonical_fragment=None,
                provenance=source,
            )
        elif accepted_candidates:
            accepted[slot_name] = accepted_candidates[0]
        elif pending_candidates:
            pending[slot_name] = pending_candidates[0]

    start = accepted.get("start_date")
    end = accepted.get("end_date")
    if start is not None and end is not None and str(end.canonical_value) < str(
        start.canonical_value
    ):
        accepted.pop("start_date", None)
        accepted.pop("end_date", None)
        rejected.extend(
            (
                RejectedCandidate("start_date", "date_range_reversed"),
                RejectedCandidate("end_date", "date_range_reversed"),
            )
        )

    return CandidateValidationResult(
        accepted=accepted,
        pending=pending,
        rejected=tuple(rejected),
    )


__all__ = [
    "HR_SLOT_VALIDATOR_VERSION",
    "HrCandidateValidationResult",
    "validate_hr_candidates",
]
