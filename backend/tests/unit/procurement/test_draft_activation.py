from policy_api.procurement.draft_activation import (
    ProcurementDraftActivationPolicy,
    has_procurement_draft_state,
)
from policy_api.slot_extraction.merge import (
    AcceptedCandidate,
    CandidateProvenance,
    CandidateValidationResult,
    PendingCandidate,
    RejectedCandidate,
)


def _provenance(*, status: str = "accepted") -> CandidateProvenance:
    return CandidateProvenance(
        source_turn_id="turn-1",
        source_kind="user_explicit",
        slot_schema_version="procurement-slot-v1",
        source_spans=((0, 2),),
        validator_version="test",
        validation_status=status,  # type: ignore[arg-type]
        match_kind="original_exact",
    )


def _validation(
    *,
    accepted: dict[str, object] | None = None,
    pending: dict[str, object] | None = None,
    rejected: tuple[RejectedCandidate, ...] = (),
) -> CandidateValidationResult:
    return CandidateValidationResult(
        accepted={
            name: AcceptedCandidate(name, value, _provenance())
            for name, value in (accepted or {}).items()
        },
        pending={
            name: PendingCandidate(
                name,
                "field_incomplete",
                value,
                _provenance(status="pending"),
            )
            for name, value in (pending or {}).items()
        },
        rejected=rejected,
    )


def test_unknown_intent_activates_from_accepted_draftable_slot() -> None:
    directive = ProcurementDraftActivationPolicy().decide(
        current_intent="unknown",
        has_active_draft=False,
        validation=_validation(accepted={"title": "办公用品"}),
    )

    assert directive.effective_intent == "draft_request"
    assert directive.should_save is True


def test_unknown_intent_activates_from_partial_item_disposition() -> None:
    directive = ProcurementDraftActivationPolicy().decide(
        current_intent="unknown",
        has_active_draft=False,
        validation=_validation(pending={
            "items": {
                "items": [
                    {
                        "fields": {"item_name": "桌子"},
                        "missing_fields": ["quantity"],
                    }
                ]
            }
        }),
    )

    assert directive.effective_intent == "draft_request"
    assert directive.should_save is True


def test_unknown_intent_does_not_activate_from_empty_item_fragment() -> None:
    directive = ProcurementDraftActivationPolicy().decide(
        current_intent="unknown",
        has_active_draft=False,
        validation=_validation(pending={
            "items": {
                "items": [
                    {
                        "fields": {},
                        "missing_fields": [
                            "item_name",
                            "quantity",
                            "unit",
                            "estimated_unit_price",
                            "category_code",
                        ],
                    }
                ]
            }
        }),
    )

    assert directive.effective_intent == "unknown"
    assert directive.should_save is False


def test_active_state_requires_a_validated_draftable_business_value() -> None:
    assert has_procurement_draft_state(
        fields={"request_id": "request-1"},
        pending={
            "items": {
                "reason_code": "item_fields_required",
                "canonical_fragment": {
                    "items": [{"fields": {}, "missing_fields": ["item_name"]}],
                },
            }
        },
    ) is False
    assert has_procurement_draft_state(
        fields={"items": [{}]},
        pending={
            "items": {
                "canonical_fragment": {
                    "items": [{"fields": {"request_id": "request-1"}}],
                },
            }
        },
    ) is False

    assert has_procurement_draft_state(
        fields={"title": "办公用品"},
        pending={},
    ) is True
    assert has_procurement_draft_state(
        fields={},
        pending={
            "items": {
                "reason_code": "item_fields_required",
                "canonical_fragment": {
                    "items": [
                        {
                            "fields": {"item_name": "桌子"},
                            "missing_fields": ["quantity"],
                        }
                    ],
                },
            }
        },
    ) is True
    assert has_procurement_draft_state(
        fields={},
        pending={
            "items": {
                "reason_code": "item_association_ambiguous",
                "canonical_fragment": {
                    "items": [],
                    "unassigned_items": [
                        {"fields": {"unit": "张"}},
                    ],
                },
            }
        },
    ) is True


def test_active_draft_continues_when_local_intent_is_unknown() -> None:
    directive = ProcurementDraftActivationPolicy().decide(
        current_intent="unknown",
        has_active_draft=True,
        validation=_validation(),
    )

    assert directive.effective_intent == "draft_request"
    assert directive.should_save is True


def test_explicit_draft_intent_stays_active_without_dispositions() -> None:
    directive = ProcurementDraftActivationPolicy().decide(
        current_intent="submit_request",
        has_active_draft=False,
        validation=_validation(),
    )

    assert directive.effective_intent == "submit_request"
    assert directive.should_save is True


def test_unknown_rejected_only_turn_does_not_activate_draft() -> None:
    directive = ProcurementDraftActivationPolicy().decide(
        current_intent="unknown",
        has_active_draft=False,
        validation=_validation(
            rejected=(RejectedCandidate("title", "source_quote_not_found"),)
        ),
    )

    assert directive.effective_intent == "unknown"
    assert directive.should_save is False


def test_unknown_action_only_slot_does_not_activate_draft() -> None:
    directive = ProcurementDraftActivationPolicy().decide(
        current_intent="unknown",
        has_active_draft=False,
        validation=_validation(accepted={"request_id": "request-1"}),
    )

    assert directive.effective_intent == "unknown"
    assert directive.should_save is False


def test_action_intent_does_not_create_business_draft_without_active_draft() -> None:
    directive = ProcurementDraftActivationPolicy().decide(
        current_intent="withdraw_request",
        has_active_draft=False,
        validation=_validation(accepted={"request_id": "request-1"}),
    )

    assert directive.effective_intent == "withdraw_request"
    assert directive.should_save is False


def test_existing_draft_is_preserved_during_action_intent() -> None:
    directive = ProcurementDraftActivationPolicy().decide(
        current_intent="request_detail",
        has_active_draft=True,
        validation=_validation(accepted={"request_id": "request-1"}),
    )

    assert directive.effective_intent == "request_detail"
    assert directive.should_save is True
