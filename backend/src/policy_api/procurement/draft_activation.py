"""Validated-slot-driven activation policy for procurement drafts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from policy_api.slot_extraction.merge import CandidateValidationResult


_DRAFT_INTENTS = frozenset({"draft_request", "submit_request"})
_DRAFTABLE_SLOTS = frozenset({
    "title",
    "purpose",
    "needed_by_date",
    "currency",
    "items",
})
_ITEM_DRAFTABLE_FIELDS = frozenset({
    "item_name",
    "specification",
    "quantity",
    "unit",
    "estimated_unit_price",
    "category_code",
})


def _item_fragment_has_validated_fields(fragment: object) -> bool:
    if not isinstance(fragment, Mapping):
        return False
    for collection_name in ("items", "unassigned_items"):
        items = fragment.get(collection_name)
        if isinstance(items, list) and any(
            isinstance(item, Mapping)
            and isinstance(item.get("fields"), Mapping)
            and bool(set(item["fields"]) & _ITEM_DRAFTABLE_FIELDS)
            for item in items
        ):
            return True
    return False


def has_procurement_draftable_disposition(
    validation: CandidateValidationResult,
) -> bool:
    if set(validation.accepted) & _DRAFTABLE_SLOTS:
        return True
    if set(validation.pending) & (_DRAFTABLE_SLOTS - {"items"}):
        return True
    item_candidate = validation.pending.get("items")
    if item_candidate is None:
        return False
    return _item_fragment_has_validated_fields(
        item_candidate.canonical_fragment
    )


def has_procurement_draft_state(
    *,
    fields: Mapping[str, object],
    pending: Mapping[str, object],
) -> bool:
    scalar_slots = _DRAFTABLE_SLOTS - {"items"}
    if set(fields) & scalar_slots or set(pending) & scalar_slots:
        return True
    stored_items = fields.get("items")
    if isinstance(stored_items, list) and any(
        isinstance(item, Mapping)
        and bool(set(item) & _ITEM_DRAFTABLE_FIELDS)
        for item in stored_items
    ):
        return True
    stored_pending = pending.get("items")
    if not isinstance(stored_pending, Mapping):
        return False
    return _item_fragment_has_validated_fields(
        stored_pending.get("canonical_fragment")
    )


@dataclass(frozen=True, slots=True)
class ProcurementDraftActivationDirective:
    effective_intent: str
    should_save: bool


class ProcurementDraftActivationPolicy:
    """Activate drafts from validated business slots, never raw phrasing alone."""

    def decide(
        self,
        *,
        current_intent: str,
        has_active_draft: bool,
        validation: CandidateValidationResult,
    ) -> ProcurementDraftActivationDirective:
        effective_intent = current_intent
        has_draftable_disposition = has_procurement_draftable_disposition(
            validation
        )

        if current_intent == "unknown" and (
            has_active_draft or has_draftable_disposition
        ):
            effective_intent = "draft_request"

        should_save = bool(
            has_active_draft
            or effective_intent in _DRAFT_INTENTS
        )
        return ProcurementDraftActivationDirective(
            effective_intent=effective_intent,
            should_save=should_save,
        )


def build_procurement_draft_activation_policy() -> ProcurementDraftActivationPolicy:
    return ProcurementDraftActivationPolicy()


__all__ = [
    "ProcurementDraftActivationDirective",
    "ProcurementDraftActivationPolicy",
    "build_procurement_draft_activation_policy",
    "has_procurement_draft_state",
    "has_procurement_draftable_disposition",
]
