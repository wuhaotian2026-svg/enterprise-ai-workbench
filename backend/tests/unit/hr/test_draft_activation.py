from importlib import import_module

import pytest

from policy_api.hr.tool_flow_policy import HrToolFlowPolicy
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
        slot_schema_version="hr-leave-slot-v1",
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
                "date_year_required",
                value,
                _provenance(status="pending"),
            )
            for name, value in (pending or {}).items()
        },
        rejected=rejected,
    )


def _policy():  # type: ignore[no-untyped-def]
    module = import_module("policy_api.hr.draft_activation")
    return module.HrDraftActivationPolicy()


@pytest.mark.parametrize(
    ("slot_name", "canonical_value"),
    [
        ("leave_type_code", "compensatory"),
        ("start_date", "2033-09-20"),
        ("end_date", "2033-09-22"),
        ("reason", "看病"),
    ],
)
def test_unknown_intent_activates_from_accepted_draftable_slot(
    slot_name: str,
    canonical_value: object,
) -> None:
    directive = _policy().decide(
        current_intent="unknown",
        has_active_draft=False,
        validation=_validation(accepted={slot_name: canonical_value}),
    )

    assert directive.effective_intent == "submit"
    assert directive.should_save is True


@pytest.mark.parametrize(
    "slot_name",
    ["date_range", "start_date", "end_date"],
)
def test_unknown_intent_activates_from_pending_date_fragment(
    slot_name: str,
) -> None:
    directive = _policy().decide(
        current_intent="unknown",
        has_active_draft=False,
        validation=_validation(
            pending={
                slot_name: {
                    "start_month": 9,
                    "start_day": 20,
                    "end_month": 9,
                    "end_day": 22,
                }
            }
        ),
    )

    assert directive.effective_intent == "submit"
    assert directive.should_save is True


@pytest.mark.parametrize(
    ("accepted", "pending"),
    [
        ({}, {}),
        ({"year": 2033}, {}),
        ({"request_id": "00000000-0000-0000-0000-000000000001"}, {}),
        ({}, {"year": {"value": 2033}}),
    ],
)
def test_helper_or_action_only_slots_do_not_activate_unknown_draft(
    accepted: dict[str, object],
    pending: dict[str, object],
) -> None:
    directive = _policy().decide(
        current_intent="unknown",
        has_active_draft=False,
        validation=_validation(accepted=accepted, pending=pending),
    )

    assert directive.effective_intent == "unknown"
    assert directive.should_save is False


def test_rejected_only_turn_does_not_activate_unknown_draft() -> None:
    directive = _policy().decide(
        current_intent="unknown",
        has_active_draft=False,
        validation=_validation(
            rejected=(RejectedCandidate("reason", "source_quote_not_found"),)
        ),
    )

    assert directive.effective_intent == "unknown"
    assert directive.should_save is False


@pytest.mark.parametrize(
    "current_intent",
    [
        "unsafe_scope",
        "cancel",
        "request_status",
        "balance",
        "duration",
        "policy",
    ],
)
def test_non_submit_intent_does_not_activate_from_incidental_candidate(
    current_intent: str,
) -> None:
    directive = _policy().decide(
        current_intent=current_intent,
        has_active_draft=False,
        validation=_validation(accepted={"reason": "看病"}),
    )

    assert directive.effective_intent == current_intent
    assert directive.should_save is False


def test_active_draft_continues_when_local_intent_is_unknown() -> None:
    directive = _policy().decide(
        current_intent="unknown",
        has_active_draft=True,
        validation=_validation(),
    )

    assert directive.effective_intent == "submit"
    assert directive.should_save is True


def test_explicit_submit_stays_active_without_dispositions() -> None:
    directive = _policy().decide(
        current_intent="submit",
        has_active_draft=False,
        validation=_validation(),
    )

    assert directive.effective_intent == "submit"
    assert directive.should_save is True


def test_real_phrase_remains_unknown_to_prove_no_regex_patch() -> None:
    assert HrToolFlowPolicy._classify_intent(
        "我想在9.20-9.22请假 用于看病"
    ) == "unknown"
