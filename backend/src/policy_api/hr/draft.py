from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from policy_api.assistant_drafts.presentation import (
    ActionableClarificationProjection,
    project_actionable_clarification,
)
from policy_api.slot_extraction.controls import DraftControl, DraftControlAction
from policy_api.slot_extraction.errors import SlotExtractionError
from policy_api.slot_extraction.merge import (
    AcceptedCandidate,
    CandidateProvenance,
    CandidateValidationResult,
    apply_pending_confirmation,
    merge_scalar,
)


_LEAVE_FIELDS = ("leave_type_code", "start_date", "end_date", "reason")
_HR_ACTIONABLE_REPLACEMENTS = {
    ("date_range", "date_year_required"): "year",
    ("start_date", "date_year_required"): "year",
    ("end_date", "date_year_required"): "year",
}


@dataclass(frozen=True, slots=True)
class HrDraftMerge:
    fields: dict[str, object]
    pending: dict[str, object]
    sources: dict[str, object]
    missing_fields: tuple[str, ...]
    clear_requested: bool = False
    intent: str = "submit_leave"


def project_hr_clarification(
    *,
    missing_fields: tuple[str, ...],
    pending: Mapping[str, object],
) -> ActionableClarificationProjection:
    """Project trusted HR draft state into actionable UI fields."""

    return project_actionable_clarification(
        missing_fields=missing_fields,
        pending=pending,
        replacements=_HR_ACTIONABLE_REPLACEMENTS,
    )


def _canonical_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _source_with_support(
    candidate: AcceptedCandidate,
    pending_entry: Mapping[str, object],
) -> dict[str, object]:
    source = candidate.provenance.to_storage()
    supporting = pending_entry.get("provenance")
    if isinstance(supporting, Mapping):
        source["supporting_sources"] = [dict(supporting)]
    return source


def _accepted_provenance_from_pending(
    pending_entry: Mapping[str, object],
) -> CandidateProvenance:
    stored = pending_entry.get("provenance")
    if not isinstance(stored, Mapping):
        raise SlotExtractionError("hr_pending_source_invalid")
    spans = stored.get("source_spans")
    if not isinstance(spans, list) or not all(
        isinstance(span, list)
        and len(span) == 2
        and all(isinstance(value, int) for value in span)
        for span in spans
    ):
        raise SlotExtractionError("hr_pending_source_invalid")
    try:
        return CandidateProvenance(
            source_turn_id=str(stored["source_turn_id"]),
            source_kind=str(stored["source_kind"]),  # type: ignore[arg-type]
            slot_schema_version=str(stored["slot_schema_version"]),
            source_spans=tuple((int(span[0]), int(span[1])) for span in spans),
            validator_version=str(stored["validator_version"]),
            validation_status="accepted",
            match_kind=str(stored["match_kind"]),  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SlotExtractionError("hr_pending_source_invalid") from exc


def _source_with_mapping_support(
    primary: CandidateProvenance,
    supporting: Mapping[str, object] | None,
) -> dict[str, object]:
    source = primary.to_storage()
    if supporting is not None:
        source["supporting_sources"] = [dict(supporting)]
    return source


def _rebase_confirmed_year(
    *,
    merged: dict[str, object],
    pending: dict[str, object],
    provenance: dict[str, object],
    previous_year: int,
    replacement_year: int,
) -> None:
    parsed: dict[str, date] = {}
    for field_name in ("start_date", "end_date"):
        raw = merged.get(field_name)
        if not isinstance(raw, str):
            return
        try:
            parsed[field_name] = date.fromisoformat(raw)
        except ValueError as exc:
            raise SlotExtractionError("hr_canonical_date_invalid") from exc
    if any(value.year != previous_year for value in parsed.values()):
        if len({value.year for value in parsed.values()}) != 1:
            for field_name in ("start_date", "end_date"):
                merged.pop(field_name, None)
                provenance.pop(field_name, None)
            pending["date_range"] = {
                "slot_name": "date_range",
                "reason_code": "date_range_year_ambiguous",
                "canonical_fragment": {
                    "start_month": parsed["start_date"].month,
                    "start_day": parsed["start_date"].day,
                    "end_month": parsed["end_date"].month,
                    "end_day": parsed["end_date"].day,
                    "provided_start_year": parsed["start_date"].year,
                    "provided_end_year": parsed["end_date"].year,
                },
            }
            return
        raise SlotExtractionError("hr_year_date_invariant_invalid")

    rebased: dict[str, str] = {}
    try:
        for field_name, value in parsed.items():
            rebased[field_name] = value.replace(year=replacement_year).isoformat()
    except ValueError:
        for field_name in ("start_date", "end_date"):
            merged.pop(field_name, None)
            provenance.pop(field_name, None)
        pending["date_range"] = {
            "slot_name": "date_range",
            "reason_code": "date_invalid_for_year",
            "canonical_fragment": {
                "start_month": parsed["start_date"].month,
                "start_day": parsed["start_date"].day,
                "end_month": parsed["end_date"].month,
                "end_day": parsed["end_date"].day,
                "provided_year": replacement_year,
            },
        }
        return

    year_source = provenance.get("year")
    for field_name, value in rebased.items():
        old_source = provenance.get(field_name)
        merged[field_name] = value
        if isinstance(year_source, Mapping):
            source = dict(year_source)
            if isinstance(old_source, Mapping):
                source["supporting_sources"] = [dict(old_source)]
            provenance[field_name] = source
        pending.pop(field_name, None)


def _reopen_inconsistent_canonical_dates(
    *,
    merged: dict[str, object],
    pending: dict[str, object],
    provenance: dict[str, object],
) -> None:
    effective_year = merged.get("year")
    if not isinstance(effective_year, int):
        return

    parsed: dict[str, date] = {}
    for field_name in ("start_date", "end_date"):
        raw = merged.get(field_name)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise SlotExtractionError("hr_canonical_date_invalid")
        try:
            parsed[field_name] = date.fromisoformat(raw)
        except ValueError as exc:
            raise SlotExtractionError("hr_canonical_date_invalid") from exc

    mismatched_names = tuple(
        field_name
        for field_name, value in parsed.items()
        if value.year != effective_year
    )
    if not mismatched_names:
        return

    if {"start_date", "end_date"} <= set(parsed):
        start = parsed["start_date"]
        end = parsed["end_date"]
        entry: dict[str, object] = {
            "slot_name": "date_range",
            "reason_code": "date_range_year_ambiguous",
            "canonical_fragment": {
                "start_month": start.month,
                "start_day": start.day,
                "end_month": end.month,
                "end_day": end.day,
                "provided_start_year": start.year,
                "provided_end_year": end.year,
            },
        }
        source = provenance.get("start_date")
        if isinstance(source, Mapping):
            entry["provenance"] = dict(source)
        for field_name in ("start_date", "end_date"):
            merged.pop(field_name, None)
            provenance.pop(field_name, None)
            pending.pop(field_name, None)
        pending["date_range"] = entry
        return

    for field_name in mismatched_names:
        value = parsed[field_name]
        entry = {
            "slot_name": field_name,
            "reason_code": "date_year_conflict",
            "canonical_fragment": {
                "month": value.month,
                "day": value.day,
                "provided_year": value.year,
            },
        }
        source = provenance.get(field_name)
        if isinstance(source, Mapping):
            entry["provenance"] = dict(source)
        merged.pop(field_name, None)
        provenance.pop(field_name, None)
        pending[field_name] = entry


def merge_hr_draft(
    *,
    fields: Mapping[str, object],
    pending: Mapping[str, object],
    sources: Mapping[str, object],
    validation: CandidateValidationResult,
    control: DraftControl | None,
    source_turn_id: str,
) -> HrDraftMerge:
    if control is not None and control.action == DraftControlAction.CLEAR_DRAFT:
        return HrDraftMerge({}, {}, {}, (), clear_requested=True)

    merged = dict(fields)
    merged_pending = dict(pending)
    provenance = dict(sources)
    previous_year = merged.get("year")

    if (
        control is not None
        and control.action
        in {DraftControlAction.CONFIRM_REPLACE, DraftControlAction.KEEP_EXISTING}
        and validation.accepted_count == 0
        and validation.pending_count == 0
        and validation.rejected_count == 0
    ):
        controlled = apply_pending_confirmation(
            preexisting=True,
            fields=merged,
            sources=provenance,
            pending=merged_pending,
            control=control,
        )
        merged = controlled.fields
        provenance = controlled.sources
        merged_pending = controlled.pending
        if (
            control.action == DraftControlAction.CONFIRM_REPLACE
            and control.field_name == "year"
            and isinstance(previous_year, int)
            and isinstance(merged.get("year"), int)
            and merged["year"] != previous_year
        ):
            _rebase_confirmed_year(
                merged=merged,
                pending=merged_pending,
                provenance=provenance,
                previous_year=previous_year,
                replacement_year=int(merged["year"]),
            )

    for slot_name, candidate in validation.accepted.items():
        if candidate.provenance.source_turn_id != source_turn_id:
            raise SlotExtractionError("candidate_source_turn_mismatch")
        if slot_name == "date_range":
            value = candidate.canonical_value
            if not isinstance(value, Mapping):
                raise SlotExtractionError("hr_date_range_candidate_invalid")
            for field_name in ("start_date", "end_date"):
                if field_name not in value:
                    raise SlotExtractionError("hr_date_range_candidate_invalid")
                expanded = AcceptedCandidate(
                    slot_name=field_name,
                    canonical_value=value[field_name],
                    provenance=candidate.provenance,
                )
                result = merge_scalar(
                    field_name=field_name,
                    current=merged.get(field_name),
                    candidate=expanded,
                    existing_source=(
                        provenance.get(field_name)
                        if isinstance(provenance.get(field_name), Mapping)
                        else None
                    ),
                    control=control,
                )
                merged[field_name] = result.value
                if result.source is not None:
                    provenance[field_name] = dict(result.source)
                if result.pending is None:
                    merged_pending.pop(field_name, None)
                else:
                    merged_pending[field_name] = result.pending
            merged_pending.pop("date_range", None)
            continue

        result = merge_scalar(
            field_name=slot_name,
            current=merged.get(slot_name),
            candidate=candidate,
            existing_source=(
                provenance.get(slot_name)
                if isinstance(provenance.get(slot_name), Mapping)
                else None
            ),
            control=control,
        )
        merged[slot_name] = result.value
        if result.source is not None:
            provenance[slot_name] = dict(result.source)
        if result.pending is None:
            merged_pending.pop(slot_name, None)
        else:
            merged_pending[slot_name] = result.pending

    for slot_name, candidate in validation.pending.items():
        merged_pending[slot_name] = candidate.to_storage()

    year_candidate = validation.accepted.get("year")
    effective_year = merged.get("year")
    if isinstance(effective_year, int):
        year = effective_year
        year_candidate_is_effective = bool(
            year_candidate is not None
            and year_candidate.canonical_value == effective_year
            and not (
                isinstance(merged_pending.get("year"), Mapping)
                and merged_pending["year"].get("reason_code")
                == "draft_value_conflict"
            )
        )
        stored_year_source = (
            provenance.get("year")
            if isinstance(provenance.get("year"), Mapping)
            else None
        )
        range_entry = merged_pending.get("date_range")
        if isinstance(range_entry, Mapping):
            fragment = range_entry.get("canonical_fragment")
            if isinstance(fragment, Mapping) and {
                "start_month",
                "start_day",
                "end_month",
                "end_day",
            } <= set(fragment):
                start_pair = (
                    int(fragment["start_month"]),
                    int(fragment["start_day"]),
                )
                end_pair = (
                    int(fragment["end_month"]),
                    int(fragment["end_day"]),
                )
                provided_year = fragment.get("provided_year")
                has_explicit_year_conflict = bool(
                    (
                        isinstance(provided_year, int)
                        and not isinstance(provided_year, bool)
                        and provided_year != year
                    )
                    or "provided_start_year" in fragment
                    or "provided_end_year" in fragment
                )
                if has_explicit_year_conflict or end_pair < start_pair:
                    ambiguous = dict(range_entry)
                    ambiguous["reason_code"] = "date_range_year_ambiguous"
                    ambiguous_fragment = dict(fragment)
                    if not has_explicit_year_conflict:
                        ambiguous_fragment["provided_year"] = year
                    ambiguous["canonical_fragment"] = ambiguous_fragment
                    merged_pending["date_range"] = ambiguous
                else:
                    start_value = _canonical_date(year, *start_pair)
                    end_value = _canonical_date(year, *end_pair)
                    if start_value is None or end_value is None:
                        invalid = dict(range_entry)
                        invalid["reason_code"] = "date_invalid_for_year"
                        merged_pending["date_range"] = invalid
                    else:
                        if (
                            year_candidate_is_effective
                            and year_candidate is not None
                        ):
                            date_provenance = year_candidate.provenance
                            support_source = _source_with_support(
                                year_candidate,
                                range_entry,
                            )
                        else:
                            date_provenance = _accepted_provenance_from_pending(
                                range_entry
                            )
                            support_source = _source_with_mapping_support(
                                date_provenance,
                                stored_year_source,
                            )
                        for field_name, value in (
                            ("start_date", start_value),
                            ("end_date", end_value),
                        ):
                            expanded = AcceptedCandidate(
                                slot_name=field_name,
                                canonical_value=value,
                                provenance=date_provenance,
                            )
                            result = merge_scalar(
                                field_name=field_name,
                                current=merged.get(field_name),
                                candidate=expanded,
                                existing_source=(
                                    provenance.get(field_name)
                                    if isinstance(
                                        provenance.get(field_name), Mapping
                                    )
                                    else None
                                ),
                                control=control,
                            )
                            merged[field_name] = result.value
                            if result.pending is None:
                                provenance[field_name] = dict(support_source)
                                merged_pending.pop(field_name, None)
                            else:
                                conflict = dict(result.pending)
                                conflict["candidate_source"] = dict(
                                    support_source
                                )
                                merged_pending[field_name] = conflict
                        merged_pending.pop("date_range", None)

        for field_name in ("start_date", "end_date"):
            entry = merged_pending.get(field_name)
            if not isinstance(entry, Mapping):
                continue
            fragment = entry.get("canonical_fragment")
            if not isinstance(fragment, Mapping) or not {"month", "day"} <= set(
                fragment
            ):
                continue
            provided_year = fragment.get("provided_year")
            if (
                isinstance(provided_year, int)
                and not isinstance(provided_year, bool)
                and provided_year != year
            ):
                continue
            value = _canonical_date(year, int(fragment["month"]), int(fragment["day"]))
            if value is None:
                invalid = dict(entry)
                invalid["reason_code"] = "date_invalid_for_year"
                merged_pending[field_name] = invalid
                continue
            if year_candidate_is_effective and year_candidate is not None:
                date_provenance = year_candidate.provenance
                date_source = _source_with_support(year_candidate, entry)
            else:
                date_provenance = _accepted_provenance_from_pending(entry)
                date_source = _source_with_mapping_support(
                    date_provenance,
                    stored_year_source,
                )
            expanded = AcceptedCandidate(
                slot_name=field_name,
                canonical_value=value,
                provenance=date_provenance,
            )
            result = merge_scalar(
                field_name=field_name,
                current=merged.get(field_name),
                candidate=expanded,
                existing_source=(
                    provenance.get(field_name)
                    if isinstance(provenance.get(field_name), Mapping)
                    else None
                ),
                control=control,
            )
            merged[field_name] = result.value
            if result.pending is None:
                provenance[field_name] = date_source
                merged_pending.pop(field_name, None)
            else:
                merged_pending[field_name] = result.pending

    _reopen_inconsistent_canonical_dates(
        merged=merged,
        pending=merged_pending,
        provenance=provenance,
    )

    if "request_id" in merged:
        missing: tuple[str, ...] = ()
    else:
        missing_names = [name for name in _LEAVE_FIELDS if name not in merged]
        date_entries = [
            merged_pending.get("date_range"),
            merged_pending.get("start_date"),
            merged_pending.get("end_date"),
        ]
        needs_year = any(
            isinstance(entry, Mapping)
            and entry.get("reason_code") == "date_year_required"
            for entry in date_entries
        )
        if needs_year:
            missing_names = [
                name
                for name in missing_names
                if name not in {"start_date", "end_date"}
            ]
            if "year" not in merged:
                missing_names.append("year")
        missing = tuple(missing_names)
    return HrDraftMerge(merged, merged_pending, provenance, missing)


__all__ = ["HrDraftMerge", "merge_hr_draft"]
