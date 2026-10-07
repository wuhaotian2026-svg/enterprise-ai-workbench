from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from policy_api.slot_extraction.controls import DraftControl, DraftControlAction
from policy_api.slot_extraction.errors import SlotExtractionError


@dataclass(frozen=True, slots=True)
class CandidateProvenance:
    source_turn_id: str
    source_kind: Literal["user_explicit", "legacy_validated"]
    slot_schema_version: str
    source_spans: tuple[tuple[int, int], ...]
    validator_version: str
    validation_status: Literal["accepted", "pending"]
    match_kind: Literal["original_exact", "controlled_normalized_exact"]

    def to_storage(self) -> dict[str, object]:
        return {
            "source_turn_id": self.source_turn_id,
            "source_kind": self.source_kind,
            "slot_schema_version": self.slot_schema_version,
            "source_spans": [list(span) for span in self.source_spans],
            "validator_version": self.validator_version,
            "validation_status": self.validation_status,
            "match_kind": self.match_kind,
        }


@dataclass(frozen=True, slots=True)
class AcceptedCandidate:
    slot_name: str
    canonical_value: object
    provenance: CandidateProvenance


@dataclass(frozen=True, slots=True)
class PendingCandidate:
    slot_name: str
    reason_code: str
    canonical_fragment: object | None
    provenance: CandidateProvenance | None

    def to_storage(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "slot_name": self.slot_name,
            "reason_code": self.reason_code,
            "canonical_fragment": self.canonical_fragment,
        }
        if self.provenance is not None:
            payload["provenance"] = self.provenance.to_storage()
        return payload


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    slot_name: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class CandidateValidationResult:
    accepted: Mapping[str, AcceptedCandidate]
    pending: Mapping[str, PendingCandidate]
    rejected: tuple[RejectedCandidate, ...]

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


@dataclass(frozen=True, slots=True)
class ScalarMergeResult:
    value: object
    source: Mapping[str, object] | None
    changed: bool
    pending: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class PendingControlMerge:
    fields: dict[str, object]
    sources: dict[str, object]
    pending: dict[str, object]


def pending_conflict(
    *,
    field_name: str,
    current_value: object,
    candidate: AcceptedCandidate,
) -> dict[str, object]:
    return {
        "field_name": field_name,
        "reason_code": "draft_value_conflict",
        "current_value": current_value,
        "candidate_value": candidate.canonical_value,
        "candidate_source": candidate.provenance.to_storage(),
    }


def merge_scalar(
    *,
    field_name: str,
    current: object | None,
    candidate: AcceptedCandidate,
    existing_source: Mapping[str, object] | None,
    control: DraftControl | None = None,
) -> ScalarMergeResult:
    candidate_source = candidate.provenance.to_storage()
    if current is None:
        return ScalarMergeResult(
            value=candidate.canonical_value,
            source=candidate_source,
            changed=True,
            pending=None,
        )
    if current == candidate.canonical_value:
        return ScalarMergeResult(
            value=current,
            source=existing_source,
            changed=False,
            pending=None,
        )
    if (
        control is not None
        and control.action == DraftControlAction.CONFIRM_REPLACE
        and control.field_name == field_name
    ):
        return ScalarMergeResult(
            value=candidate.canonical_value,
            source=candidate_source,
            changed=True,
            pending=None,
        )
    return ScalarMergeResult(
        value=current,
        source=existing_source,
        changed=False,
        pending=pending_conflict(
            field_name=field_name,
            current_value=current,
            candidate=candidate,
        ),
    )


def apply_pending_confirmation(
    *,
    preexisting: bool,
    fields: Mapping[str, object],
    sources: Mapping[str, object],
    pending: Mapping[str, object],
    control: DraftControl,
) -> PendingControlMerge:
    if not preexisting:
        raise SlotExtractionError("draft_conflict_confirmation_not_prior")
    if control.action not in {
        DraftControlAction.CONFIRM_REPLACE,
        DraftControlAction.KEEP_EXISTING,
    }:
        raise SlotExtractionError("draft_conflict_control_invalid")

    conflicts = {
        name: value
        for name, value in pending.items()
        if isinstance(value, Mapping)
        and value.get("reason_code") == "draft_value_conflict"
    }
    field_name = control.field_name
    if field_name is None:
        if len(conflicts) != 1:
            raise SlotExtractionError("draft_conflict_confirmation_ambiguous")
        field_name = next(iter(conflicts))
    conflict = conflicts.get(field_name)
    if conflict is None:
        raise SlotExtractionError("draft_conflict_not_found")

    merged_fields = dict(fields)
    merged_sources = dict(sources)
    merged_pending = dict(pending)
    if control.action == DraftControlAction.CONFIRM_REPLACE:
        merged_fields[field_name] = conflict["candidate_value"]
        candidate_source = conflict.get("candidate_source")
        if not isinstance(candidate_source, Mapping):
            raise SlotExtractionError("draft_conflict_source_invalid")
        merged_sources[field_name] = dict(candidate_source)
    merged_pending.pop(field_name, None)
    return PendingControlMerge(
        fields=merged_fields,
        sources=merged_sources,
        pending=merged_pending,
    )


__all__ = [
    "AcceptedCandidate",
    "CandidateProvenance",
    "CandidateValidationResult",
    "PendingCandidate",
    "PendingControlMerge",
    "RejectedCandidate",
    "ScalarMergeResult",
    "apply_pending_confirmation",
    "merge_scalar",
    "pending_conflict",
]
