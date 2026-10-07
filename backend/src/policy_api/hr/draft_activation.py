"""Validated-slot-driven activation policy for HR leave drafts."""

from __future__ import annotations

from dataclasses import dataclass

from policy_api.slot_extraction.merge import CandidateValidationResult


_DRAFTABLE_ACCEPTED_SLOTS = frozenset({
    "leave_type_code",
    "start_date",
    "end_date",
    "date_range",
    "reason",
})
_DRAFTABLE_PENDING_SLOTS = frozenset({
    "start_date",
    "end_date",
    "date_range",
})


def has_hr_draftable_disposition(
    validation: CandidateValidationResult,
) -> bool:
    return bool(
        set(validation.accepted) & _DRAFTABLE_ACCEPTED_SLOTS
        or set(validation.pending) & _DRAFTABLE_PENDING_SLOTS
    )


@dataclass(frozen=True, slots=True)
class HrDraftActivationDirective:
    effective_intent: str
    should_save: bool


class HrDraftActivationPolicy:
    """Activate leave drafts from validated business slots, never raw phrasing."""

    def decide(
        self,
        *,
        current_intent: str,
        has_active_draft: bool,
        validation: CandidateValidationResult,
    ) -> HrDraftActivationDirective:
        effective_intent = current_intent
        if current_intent == "unknown" and (
            has_active_draft or has_hr_draftable_disposition(validation)
        ):
            effective_intent = "submit"

        return HrDraftActivationDirective(
            effective_intent=effective_intent,
            should_save=bool(
                has_active_draft or effective_intent == "submit"
            ),
        )


def build_hr_draft_activation_policy() -> HrDraftActivationPolicy:
    return HrDraftActivationPolicy()


__all__ = [
    "HrDraftActivationDirective",
    "HrDraftActivationPolicy",
    "build_hr_draft_activation_policy",
    "has_hr_draftable_disposition",
]
