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
    CandidateValidationResult,
    apply_pending_confirmation,
    merge_scalar,
)


_REQUIRED_FIELDS = ("title", "purpose", "needed_by_date", "currency", "items")
_ITEM_REQUIRED = (
    "item_name",
    "quantity",
    "unit",
    "estimated_unit_price",
    "category_code",
)
_PROCUREMENT_ACTIONABLE_REPLACEMENTS = {
    ("needed_by_date", "date_year_required"): "needed_by_year",
}


@dataclass(frozen=True, slots=True)
class ProcurementDraftMerge:
    fields: dict[str, object]
    pending: dict[str, object]
    sources: dict[str, object]
    missing_fields: tuple[str, ...]
    clear_requested: bool = False
    intent: str = "draft_request"
    explicit_submit: bool = False


def _validated_pending_source(entry: Mapping[str, object]) -> dict[str, object]:
    source = entry.get("provenance")
    if not isinstance(source, Mapping):
        raise SlotExtractionError("procurement_pending_source_invalid")
    spans = source.get("source_spans")
    if (
        not isinstance(source.get("source_turn_id"), str)
        or source.get("source_kind") not in {"user_explicit", "legacy_validated"}
        or not isinstance(source.get("slot_schema_version"), str)
        or not isinstance(source.get("validator_version"), str)
        or source.get("validation_status") != "pending"
        or source.get("match_kind")
        not in {"original_exact", "controlled_normalized_exact"}
        or not isinstance(spans, list)
        or not all(
            isinstance(span, list)
            and len(span) == 2
            and all(isinstance(value, int) for value in span)
            for span in spans
        )
    ):
        raise SlotExtractionError("procurement_pending_source_invalid")
    return dict(source)


def _date_candidate_source(
    year_candidate: AcceptedCandidate,
    pending_date: Mapping[str, object],
) -> dict[str, object]:
    source = year_candidate.provenance.to_storage()
    source["supporting_sources"] = [_validated_pending_source(pending_date)]
    return source


def _is_precise_item_leaf_path(name: str) -> bool:
    if not name.startswith("items["):
        return False
    index, separator, leaf = name[len("items["):].partition("].")
    return bool(
        separator
        and index.isascii()
        and index.isdecimal()
        and leaf
        and not any(character in leaf for character in ".[]")
    )


def project_procurement_clarification(
    *,
    missing_fields: tuple[str, ...],
    pending: Mapping[str, object],
) -> ActionableClarificationProjection:
    """Project trusted procurement draft state into actionable UI fields."""

    has_precise_item_path = any(
        _is_precise_item_leaf_path(name) for name in missing_fields
    )
    covered_pending_fields = ("items",) if has_precise_item_path else ()
    return project_actionable_clarification(
        missing_fields=missing_fields,
        pending=pending,
        replacements=_PROCUREMENT_ACTIONABLE_REPLACEMENTS,
        covered_pending_fields=covered_pending_fields,
    )


def project_procurement_clarification_fields(
    *,
    missing_fields: tuple[str, ...],
    pending: Mapping[str, object],
) -> tuple[str, ...]:
    """Compatibility projection for evaluators and existing callers."""

    return project_procurement_clarification(
        missing_fields=missing_fields,
        pending=pending,
    ).clarification_fields


def _copy_partial_item(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    item_ref = value.get("item_ref")
    fields = value.get("fields")
    field_sources = value.get("field_sources")
    missing_fields = value.get("missing_fields")
    if (
        not isinstance(item_ref, str)
        or not isinstance(fields, Mapping)
        or not isinstance(field_sources, Mapping)
        or not isinstance(missing_fields, list)
        or not all(isinstance(name, str) for name in missing_fields)
    ):
        return None
    copied: dict[str, object] = {
        "item_ref": item_ref,
        "fields": dict(fields),
        "field_sources": {
            str(name): dict(source) if isinstance(source, Mapping) else source
            for name, source in field_sources.items()
        },
        "missing_fields": list(missing_fields),
    }
    rejected = value.get("rejected_fields")
    if isinstance(rejected, list):
        copied["rejected_fields"] = [
            dict(item) for item in rejected if isinstance(item, Mapping)
        ]
    conflicts = value.get("conflicts")
    if isinstance(conflicts, Mapping):
        copied["conflicts"] = {
            str(name): dict(conflict)
            for name, conflict in conflicts.items()
            if isinstance(conflict, Mapping)
        }
    unresolved_reason_code = value.get("unresolved_reason_code")
    if isinstance(unresolved_reason_code, str):
        copied["unresolved_reason_code"] = unresolved_reason_code
    return copied


def _partial_items_from_storage(value: object) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not isinstance(value, Mapping):
        return [], []
    fragment = value.get("canonical_fragment")
    if not isinstance(fragment, Mapping):
        return [], []
    raw_items = fragment.get("items")
    items = []
    if isinstance(raw_items, list):
        items = [
            _normalize_partial_record(copied)
            for raw in raw_items
            if (copied := _copy_partial_item(raw)) is not None
        ]
    elif isinstance(fragment.get("item_name"), str):
        item_name = str(fragment["item_name"]).strip()
        stored_source = value.get("provenance")
        source = dict(stored_source) if isinstance(stored_source, Mapping) else {}
        items = [{
            "item_ref": f"legacy:{source.get('source_turn_id', 'unknown')}",
            "fields": {"item_name": item_name},
            "field_sources": {"item_name": source},
            "missing_fields": [
                name for name in _ITEM_REQUIRED if name != "item_name"
            ],
        }]
    raw_unassigned = fragment.get("unassigned_items")
    unassigned = []
    if isinstance(raw_unassigned, list):
        unassigned = [
            _normalize_partial_record(copied)
            for raw in raw_unassigned
            if (copied := _copy_partial_item(raw)) is not None
        ]
    return items, unassigned


def _incoming_partial_items(validation: CandidateValidationResult) -> list[dict[str, object]]:
    candidate = validation.pending.get("items")
    if candidate is None:
        return []
    fragment = candidate.canonical_fragment
    if not isinstance(fragment, Mapping):
        return []
    raw_items = fragment.get("items")
    if not isinstance(raw_items, list):
        return []
    return [
        _normalize_partial_record(copied)
        for raw in raw_items
        if (copied := _copy_partial_item(raw)) is not None
    ]


def _record_from_complete_item(
    *,
    value: object,
    source: Mapping[str, object],
    source_turn_id: str,
    index: int,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or not all(
        name in value for name in _ITEM_REQUIRED
    ):
        raise SlotExtractionError("procurement_complete_item_invalid")
    fields = {
        name: value[name]
        for name in (
            "category_code",
            "item_name",
            "specification",
            "quantity",
            "unit",
            "estimated_unit_price",
        )
        if name in value
        and not (name == "specification" and value[name] is None)
    }
    return {
        "item_ref": f"complete:{source_turn_id}:{index}",
        "fields": fields,
        "field_sources": {
            name: dict(source)
            for name in fields
        },
        "missing_fields": [],
    }


def _record_is_complete(record: Mapping[str, object]) -> bool:
    fields = record.get("fields")
    conflicts = record.get("conflicts")
    return bool(
        isinstance(fields, Mapping)
        and all(name in fields for name in _ITEM_REQUIRED)
        and not conflicts
        and not record.get("unresolved_reason_code")
        and not _has_blocking_rejection(record)
    )


def _is_redundant_complete_item_reference(
    *,
    complete_items: list[dict[str, object]],
    incoming: Mapping[str, object],
) -> bool:
    fields = incoming.get("fields")
    if (
        not isinstance(fields, Mapping)
        or set(fields) != {"item_name"}
        or incoming.get("conflicts")
        or incoming.get("rejected_fields")
    ):
        return False
    item_name = fields.get("item_name")
    if not isinstance(item_name, str) or not item_name:
        return False
    return sum(
        item.get("item_name") == item_name
        for item in complete_items
    ) == 1


def _canonical_item(record: Mapping[str, object]) -> dict[str, object]:
    fields = record.get("fields")
    if not isinstance(fields, Mapping) or not _record_is_complete(record):
        raise SlotExtractionError("procurement_partial_item_not_complete")
    return {
        "category_code": fields["category_code"],
        "item_name": fields["item_name"],
        "specification": fields.get("specification"),
        "quantity": fields["quantity"],
        "unit": fields["unit"],
        "estimated_unit_price": fields["estimated_unit_price"],
    }


def _rejected_item_field_name(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    field_path = value.get("field_path")
    if not isinstance(field_path, str):
        return None
    _prefix, separator, field_name = field_path.rpartition(".")
    if separator and field_name in {*_ITEM_REQUIRED, "specification"}:
        return field_name
    return None


def _has_blocking_rejection(record: Mapping[str, object]) -> bool:
    fields = record.get("fields")
    if not isinstance(fields, Mapping):
        return True
    rejected = record.get("rejected_fields")
    if not isinstance(rejected, list):
        return False
    for entry in rejected:
        field_name = _rejected_item_field_name(entry)
        if field_name is None:
            return True
        if field_name in _ITEM_REQUIRED and field_name not in fields:
            return True
    return False


def _normalize_partial_record(record: Mapping[str, object]) -> dict[str, object]:
    normalized = _copy_partial_item(record)
    if normalized is None:
        raise SlotExtractionError("procurement_partial_item_invalid")
    fields = normalized["fields"]
    if not isinstance(fields, Mapping):
        raise SlotExtractionError("procurement_partial_item_invalid")
    normalized["missing_fields"] = [
        name for name in _ITEM_REQUIRED if name not in fields
    ]
    rejected = normalized.get("rejected_fields")
    if isinstance(rejected, list):
        actionable = []
        for entry in rejected:
            field_name = _rejected_item_field_name(entry)
            if field_name is None or (
                field_name in _ITEM_REQUIRED and field_name not in fields
            ):
                actionable.append(entry)
        if actionable:
            normalized["rejected_fields"] = actionable
        else:
            normalized.pop("rejected_fields", None)
    return normalized


def _promoted_item_source_projection(
    records: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "items": [
            {
                "item_ref": record["item_ref"],
                "field_sources": {
                    str(name): dict(source)
                    for name, source in dict(record["field_sources"]).items()
                    if isinstance(source, Mapping)
                },
            }
            for record in records
        ]
    }


def _merge_partial_record(
    existing: Mapping[str, object],
    incoming: Mapping[str, object],
) -> dict[str, object]:
    merged = _copy_partial_item(existing)
    current = _copy_partial_item(incoming)
    if merged is None or current is None:
        raise SlotExtractionError("procurement_partial_item_invalid")
    merged_fields = dict(merged["fields"])
    merged_sources = dict(merged["field_sources"])
    conflicts = dict(merged.get("conflicts", {}))
    for name, value in dict(current["fields"]).items():
        if name not in merged_fields:
            merged_fields[name] = value
            if name in current["field_sources"]:
                merged_sources[name] = current["field_sources"][name]
        elif merged_fields[name] != value:
            conflicts[name] = {
                "current_value": merged_fields[name],
                "candidate_value": value,
                "candidate_source": current["field_sources"].get(name),
            }
    merged["fields"] = merged_fields
    merged["field_sources"] = merged_sources
    merged["missing_fields"] = [
        name for name in _ITEM_REQUIRED if name not in merged_fields
    ]
    rejected = [
        entry
        for entry in list(merged.get("rejected_fields", []))
    ]
    rejected.extend(list(current.get("rejected_fields", [])))
    merged["rejected_fields"] = rejected
    if conflicts:
        merged["conflicts"] = conflicts
    else:
        merged.pop("conflicts", None)
    current_reason = current.get("unresolved_reason_code")
    if isinstance(current_reason, str):
        merged["unresolved_reason_code"] = current_reason
    elif current["fields"]:
        merged.pop("unresolved_reason_code", None)
    return _normalize_partial_record(merged)


def _association_target(
    existing: list[dict[str, object]],
    incoming: Mapping[str, object],
) -> tuple[int | None, bool]:
    incoming_fields = incoming.get("fields")
    incoming_name = (
        incoming_fields.get("item_name")
        if isinstance(incoming_fields, Mapping)
        else None
    )
    if isinstance(incoming_name, str):
        matches = [
            index
            for index, item in enumerate(existing)
            if isinstance(item.get("fields"), Mapping)
            and item["fields"].get("item_name") == incoming_name
        ]
        if len(matches) == 1:
            return matches[0], False
        if len(matches) > 1:
            return None, True
        if len(existing) == 1:
            existing_name = existing[0].get("fields", {}).get("item_name")  # type: ignore[union-attr]
            if existing_name is None:
                return 0, False
        return None, False
    if len(existing) == 1:
        return 0, False
    return None, len(existing) > 1


def _inherit_omitted_specifications(
    *,
    existing: list[dict[str, object]],
    candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    existing_records = [{"fields": item} for item in existing]
    inherited: list[dict[str, object]] = []
    for value in candidates:
        candidate = dict(value)
        if candidate.get("specification") is None:
            target, ambiguous = _association_target(
                existing_records,
                {"fields": candidate},
            )
            candidate_name = candidate.get("item_name")
            if (
                target is not None
                and not ambiguous
                and isinstance(candidate_name, str)
                and existing[target].get("item_name") == candidate_name
            ):
                if "specification" not in existing[target]:
                    candidate.pop("specification", None)
                else:
                    specification = existing[target].get("specification")
                    if specification is not None:
                        candidate["specification"] = specification
        inherited.append(candidate)
    return inherited


def _pending_item_storage(
    *,
    items: list[dict[str, object]],
    unassigned: list[dict[str, object]],
    reason_code: str,
    provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    fragment: dict[str, object] = {"items": items}
    if unassigned:
        fragment["unassigned_items"] = unassigned
    stored: dict[str, object] = {
        "slot_name": "items",
        "reason_code": reason_code,
        "canonical_fragment": fragment,
    }
    if provenance is not None:
        stored["provenance"] = dict(provenance)
    return stored


def _item_missing_paths(
    *,
    complete_count: int,
    items: list[dict[str, object]],
    unassigned: list[dict[str, object]],
) -> list[str]:
    paths: list[str] = []
    for offset, item in enumerate((*items, *unassigned)):
        index = complete_count + offset
        fields = item.get("fields")
        if (
            isinstance(fields, Mapping)
            and not fields
            and (
                item.get("unresolved_reason_code")
                or item.get("rejected_fields")
            )
        ):
            paths.append(f"items[{index}]")
            continue
        names = list(item.get("missing_fields", []))
        conflicts = item.get("conflicts")
        if isinstance(conflicts, Mapping):
            names.extend(str(name) for name in conflicts)
        for name in dict.fromkeys(str(name) for name in names):
            paths.append(f"items[{index}].{name}")
        if not names and item.get("rejected_fields"):
            paths.append(f"items[{index}]")
    return paths


def merge_procurement_draft(
    *,
    fields: Mapping[str, object],
    pending: Mapping[str, object],
    sources: Mapping[str, object],
    validation: CandidateValidationResult,
    control: DraftControl | None,
    source_turn_id: str,
    explicit_submit: bool = False,
    today: date | None = None,
) -> ProcurementDraftMerge:
    if control is not None and control.action == DraftControlAction.CLEAR_DRAFT:
        return ProcurementDraftMerge({}, {}, {}, (), clear_requested=True)

    merged = dict(fields)
    merged_pending = dict(pending)
    provenance = dict(sources)

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

    for slot_name, candidate in validation.accepted.items():
        if candidate.provenance.source_turn_id != source_turn_id:
            raise SlotExtractionError("candidate_source_turn_mismatch")
        if slot_name in {"items", "needed_by_year"}:
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
        if slot_name in {"items", "needed_by_year"}:
            continue
        merged_pending[slot_name] = candidate.to_storage()

    needed_year = validation.accepted.get("needed_by_year")
    pending_date = merged_pending.get("needed_by_date")
    if needed_year is not None and isinstance(pending_date, Mapping):
        fragment = pending_date.get("canonical_fragment")
        if (
            pending_date.get("reason_code") == "date_year_required"
            and isinstance(fragment, Mapping)
            and isinstance(fragment.get("month"), int)
            and not isinstance(fragment.get("month"), bool)
            and isinstance(fragment.get("day"), int)
            and not isinstance(fragment.get("day"), bool)
        ):
            if today is None:
                raise SlotExtractionError("procurement_current_date_required")
            year = needed_year.canonical_value
            if not isinstance(year, int) or isinstance(year, bool):
                raise SlotExtractionError("procurement_needed_year_invalid")
            month = int(fragment["month"])
            day = int(fragment["day"])
            candidate_source = _date_candidate_source(needed_year, pending_date)
            try:
                value = date(year, month, day)
            except ValueError:
                updated_fragment = dict(fragment)
                updated_fragment["provided_year"] = year
                updated_pending = dict(pending_date)
                updated_pending["reason_code"] = "date_invalid_for_year"
                updated_pending["canonical_fragment"] = updated_fragment
                updated_pending["candidate_source"] = candidate_source
                merged_pending["needed_by_date"] = updated_pending
            else:
                if value < today:
                    updated_fragment = dict(fragment)
                    updated_fragment["provided_year"] = year
                    updated_pending = dict(pending_date)
                    updated_pending["reason_code"] = (
                        "needed_by_date_before_today"
                    )
                    updated_pending["canonical_fragment"] = updated_fragment
                    updated_pending["candidate_source"] = candidate_source
                    merged_pending["needed_by_date"] = updated_pending
                else:
                    expanded = AcceptedCandidate(
                        slot_name="needed_by_date",
                        canonical_value=value.isoformat(),
                        provenance=needed_year.provenance,
                    )
                    result = merge_scalar(
                        field_name="needed_by_date",
                        current=merged.get("needed_by_date"),
                        candidate=expanded,
                        existing_source=(
                            provenance.get("needed_by_date")
                            if isinstance(
                                provenance.get("needed_by_date"),
                                Mapping,
                            )
                            else None
                        ),
                        control=control,
                    )
                    merged["needed_by_date"] = result.value
                    if result.pending is None:
                        provenance["needed_by_date"] = candidate_source
                        merged_pending.pop("needed_by_date", None)
                    else:
                        conflict = dict(result.pending)
                        conflict["candidate_source"] = candidate_source
                        merged_pending["needed_by_date"] = conflict

    accepted_items = validation.accepted.get("items")
    pending_items = validation.pending.get("items")
    if accepted_items is not None and (
        accepted_items.provenance.source_turn_id != source_turn_id
    ):
        raise SlotExtractionError("candidate_source_turn_mismatch")
    if (
        pending_items is not None
        and pending_items.provenance is not None
        and pending_items.provenance.source_turn_id != source_turn_id
    ):
        raise SlotExtractionError("candidate_source_turn_mismatch")

    has_item_disposition = accepted_items is not None or pending_items is not None
    stored_partial_records, stored_unassigned_records = _partial_items_from_storage(
        merged_pending.get("items")
    )
    has_stored_item_state = bool(
        stored_partial_records or stored_unassigned_records
    )
    if has_item_disposition or has_stored_item_state:
        existing_complete_raw = merged.get("items")
        if existing_complete_raw is None:
            complete_items: list[dict[str, object]] = []
        elif isinstance(existing_complete_raw, list) and all(
            isinstance(item, Mapping) for item in existing_complete_raw
        ):
            complete_items = [dict(item) for item in existing_complete_raw]
        else:
            raise SlotExtractionError("procurement_draft_items_invalid")

        partial_records = stored_partial_records
        unassigned_records = stored_unassigned_records
        incoming_records = _incoming_partial_items(validation)
        if (
            pending_items is not None
            and not incoming_records
            and pending_items.canonical_fragment is None
        ):
            incoming_records = [{
                "item_ref": f"unresolved:{source_turn_id}:items",
                "fields": {},
                "field_sources": {},
                "missing_fields": list(_ITEM_REQUIRED),
                "unresolved_reason_code": pending_items.reason_code,
            }]
        if not partial_records and not unassigned_records:
            incoming_records = [
                incoming
                for incoming in incoming_records
                if not _is_redundant_complete_item_reference(
                    complete_items=complete_items,
                    incoming=incoming,
                )
            ]
        accepted_records: list[dict[str, object]] = []
        if accepted_items is not None:
            if not isinstance(accepted_items.canonical_value, list):
                raise SlotExtractionError("procurement_complete_items_invalid")
            source = accepted_items.provenance.to_storage()
            accepted_records = [
                _record_from_complete_item(
                    value=value,
                    source=source,
                    source_turn_id=source_turn_id,
                    index=index,
                )
                for index, value in enumerate(accepted_items.canonical_value)
            ]

        stored_items_pending = merged_pending.get("items")
        association_ambiguous = bool(
            isinstance(stored_items_pending, Mapping)
            and stored_items_pending.get("reason_code")
            == "item_association_ambiguous"
        )
        unmatched_complete: list[dict[str, object]] = []
        for incoming in (*accepted_records, *incoming_records):
            target, ambiguous = _association_target(partial_records, incoming)
            if target is not None:
                partial_records[target] = _merge_partial_record(
                    partial_records[target],
                    incoming,
                )
                continue
            if ambiguous:
                unassigned_records.append(incoming)
                association_ambiguous = True
                continue
            if _record_is_complete(incoming):
                unmatched_complete.append(incoming)
            else:
                partial_records.append(incoming)

        promoted: list[dict[str, object]] = []
        promoted_records: list[dict[str, object]] = []
        remaining_partial: list[dict[str, object]] = []
        for record in partial_records:
            if _record_is_complete(record):
                promoted.append(_canonical_item(record))
                promoted_records.append(record)
            else:
                remaining_partial.append(record)
        partial_records = remaining_partial

        if unmatched_complete:
            candidate_values = _inherit_omitted_specifications(
                existing=complete_items,
                candidates=[
                    _canonical_item(record) for record in unmatched_complete
                ],
            )
            if not complete_items:
                complete_items.extend(candidate_values)
            elif candidate_values != complete_items:
                if accepted_items is None:
                    raise SlotExtractionError("procurement_complete_items_invalid")
                conflict = merge_scalar(
                    field_name="items",
                    current=complete_items,
                    candidate=AcceptedCandidate(
                        slot_name="items",
                        canonical_value=candidate_values,
                        provenance=accepted_items.provenance,
                    ),
                    existing_source=(
                        provenance.get("items")
                        if isinstance(provenance.get("items"), Mapping)
                        else None
                    ),
                    control=control,
                )
                complete_items = list(conflict.value)  # type: ignore[arg-type]
                if conflict.source is not None:
                    provenance["items"] = dict(conflict.source)
                if conflict.pending is not None:
                    merged_pending["items"] = conflict.pending

        if promoted:
            complete_items.extend(promoted)
        if complete_items:
            merged["items"] = complete_items
            if promoted_records:
                promoted_sources = _promoted_item_source_projection(
                    promoted_records
                )
                existing_item_source = provenance.get("items")
                if isinstance(existing_item_source, Mapping):
                    promoted_sources["aggregate_source"] = dict(
                        existing_item_source
                    )
                provenance["items"] = promoted_sources
            elif accepted_items is not None and "items" not in provenance:
                provenance["items"] = accepted_items.provenance.to_storage()
        else:
            merged.pop("items", None)

        has_whole_conflict = bool(
            isinstance(merged_pending.get("items"), Mapping)
            and merged_pending["items"].get("reason_code")
            == "draft_value_conflict"
        )
        if partial_records or unassigned_records:
            pending_source = (
                pending_items.provenance.to_storage()
                if pending_items is not None
                and pending_items.provenance is not None
                else (
                    dict(stored_items_pending["provenance"])
                    if isinstance(stored_items_pending, Mapping)
                    and isinstance(
                        stored_items_pending.get("provenance"), Mapping
                    )
                    else None
                )
            )
            has_leaf_conflict = any(
                bool(record.get("conflicts"))
                for record in (*partial_records, *unassigned_records)
            )
            has_source_ambiguity = any(
                record.get("unresolved_reason_code")
                == "source_quote_ambiguous"
                for record in (*partial_records, *unassigned_records)
            )
            structured = _pending_item_storage(
                items=partial_records,
                unassigned=unassigned_records,
                reason_code=(
                    "item_association_ambiguous"
                    if association_ambiguous
                    else (
                        "source_quote_ambiguous"
                        if has_source_ambiguity
                        else (
                            "item_leaf_conflict"
                            if has_leaf_conflict
                            else "item_fields_required"
                        )
                    )
                ),
                provenance=pending_source,
            )
            if has_whole_conflict:
                merged_pending["items"]["canonical_fragment"] = structured[
                    "canonical_fragment"
                ]
            else:
                merged_pending["items"] = structured
        elif not has_whole_conflict:
            merged_pending.pop("items", None)

    if "request_id" in merged or "task_id" in merged:
        missing: tuple[str, ...] = ()
    else:
        missing_names = [
            name for name in _REQUIRED_FIELDS if name != "items" and name not in merged
        ]
        stored_partial, stored_unassigned = _partial_items_from_storage(
            merged_pending.get("items")
        )
        complete_count = (
            len(merged["items"])
            if isinstance(merged.get("items"), list)
            else 0
        )
        leaf_paths = _item_missing_paths(
            complete_count=complete_count,
            items=stored_partial,
            unassigned=stored_unassigned,
        )
        if leaf_paths:
            missing_names.extend(leaf_paths)
        elif "items" not in merged:
            missing_names.append("items")
        elif isinstance(merged_pending.get("items"), Mapping):
            missing_names.append("items")
        missing = tuple(dict.fromkeys(missing_names))
    return ProcurementDraftMerge(
        fields=merged,
        pending=merged_pending,
        sources=provenance,
        missing_fields=missing,
        intent="submit_request" if explicit_submit else "draft_request",
        explicit_submit=explicit_submit,
    )


__all__ = [
    "ProcurementDraftMerge",
    "merge_procurement_draft",
    "project_procurement_clarification_fields",
]
