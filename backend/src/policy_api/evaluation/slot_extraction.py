"""Deterministic contracts and scoring for candidate Slot Extraction."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


PUBLIC_CATEGORY_DISTRIBUTION = {
    "natural_multifield": 10,
    "followup": 5,
    "ambiguity": 4,
    "conflict": 4,
    "invalid": 3,
    "adversarial": 4,
}
MODULES = frozenset({"hr", "procurement"})
CATEGORIES = frozenset(PUBLIC_CATEGORY_DISTRIBUTION)
METRIC_NAMES = frozenset({
    "envelope_valid",
    "field_precision",
    "field_recall",
    "ambiguity_handled",
    "source_verified",
    "clarification_correct",
    "terminal_correct",
    "must_not_execute",
    "call_budget_respected",
})


class SlotExtractionInputError(ValueError):
    pass


class SlotExtractionCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=100)
    module: Literal["hr", "procurement"]
    category: Literal[
        "natural_multifield", "followup", "ambiguity", "conflict",
        "invalid", "adversarial", "ambiguity_conflict",
        "adversarial_idempotency",
    ]
    current_user_turn: str = Field(min_length=1, max_length=2000)
    initial_draft: dict[str, object] = Field(default_factory=dict)
    initial_pending: dict[str, object] = Field(default_factory=dict)
    initial_sources: dict[str, object] = Field(default_factory=dict)
    replay_same_turn: bool = False


class RejectedExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    slot_name: str = Field(min_length=1, max_length=80)
    reason_code: str = Field(min_length=1, max_length=100)


class SlotExtractionExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    case_id: str = Field(min_length=1, max_length=100)
    module: Literal["hr", "procurement"]
    category: Literal[
        "natural_multifield", "followup", "ambiguity", "conflict",
        "invalid", "adversarial", "ambiguity_conflict",
        "adversarial_idempotency",
    ]
    applicable_metrics: list[str] = Field(min_length=1)
    expected_fields: dict[str, object]
    expected_pending: dict[str, str]
    expected_rejected: list[RejectedExpectation]
    expected_clarification_fields: list[str] = Field(default_factory=list)
    expected_terminal: Literal["accepted", "clarification", "rejected", "error"]
    must_not_execute: bool = True


def _normalize_expected_canonical_types(
    expectation: SlotExtractionExpectation,
) -> SlotExtractionExpectation:
    fields = dict(expectation.expected_fields)
    year = fields.get("year")
    if (
        expectation.module == "hr"
        and isinstance(year, str)
        and re.fullmatch(r"\d{4}", year) is not None
    ):
        fields["year"] = int(year)
    return expectation.model_copy(update={"expected_fields": fields})


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    slot_name: str
    source_turn_id: str
    match_kind: str
    source_spans: tuple[tuple[int, int], ...]
    verified: bool


@dataclass(frozen=True, slots=True)
class SlotExtractionTrace:
    envelope_valid: bool
    accepted_fields: dict[str, object]
    pending: dict[str, str]
    rejected: tuple[tuple[str, str], ...]
    canonical_fields: dict[str, object]
    clarification_fields: tuple[str, ...]
    terminal: str
    source_evidence: tuple[SourceEvidence, ...] = ()
    extraction_calls: int = 0
    extraction_latency_ms: int = 0
    incomplete_turn_latency_ms: int = 0
    total_turn_latency_ms: int = 0
    write_executed: int = 0
    created_resources: int = 0
    duplicate_resources: int = 0
    error_code: str | None = None

    @classmethod
    def fixture_from_expectation(
        cls, expectation: SlotExtractionExpectation
    ) -> SlotExtractionTrace:
        accepted = dict(expectation.expected_fields)
        evidence = tuple(
            SourceEvidence(
                slot_name=name,
                source_turn_id="fixture-turn",
                match_kind="original_exact",
                source_spans=((0, 1),),
                verified=True,
            )
            for name in accepted
        )
        return cls(
            envelope_valid=True,
            accepted_fields=accepted,
            pending=dict(expectation.expected_pending),
            rejected=tuple(
                (item.slot_name, item.reason_code)
                for item in expectation.expected_rejected
            ),
            canonical_fields=accepted,
            clarification_fields=tuple(expectation.expected_clarification_fields),
            terminal=expectation.expected_terminal,
            source_evidence=evidence,
            extraction_calls=1,
        )


def _load_json(path: str | Path, *, label: str) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SlotExtractionInputError(f"invalid_{label}_json") from exc
    if not isinstance(payload, dict):
        raise SlotExtractionInputError(f"invalid_{label}_catalog")
    return payload


def load_slot_extraction_catalog(
    cases_path: str | Path,
    manifest_path: str | Path,
) -> tuple[list[SlotExtractionCase], dict[str, SlotExtractionExpectation]]:
    cases_payload = _load_json(cases_path, label="slot_extraction_cases")
    manifest_payload = _load_json(manifest_path, label="slot_extraction_manifest")
    if cases_payload.get("schema_version") != 1:
        raise SlotExtractionInputError("invalid_slot_extraction_cases_version")
    if manifest_payload.get("schema_version") != 1:
        raise SlotExtractionInputError("invalid_slot_extraction_manifest_version")
    raw_cases = cases_payload.get("cases")
    raw_manifest = manifest_payload.get("cases")
    if not isinstance(raw_cases, list) or not isinstance(raw_manifest, list):
        raise SlotExtractionInputError("invalid_slot_extraction_catalog")
    try:
        cases = [SlotExtractionCase.model_validate(item) for item in raw_cases]
        expectations = [
            _normalize_expected_canonical_types(
                SlotExtractionExpectation.model_validate(item)
            )
            for item in raw_manifest
        ]
    except Exception as exc:
        raise SlotExtractionInputError("invalid_slot_extraction_contract") from exc
    case_ids = [case.id for case in cases]
    expectation_ids = [item.case_id for item in expectations]
    if (
        len(case_ids) != len(set(case_ids))
        or len(expectation_ids) != len(set(expectation_ids))
        or set(case_ids) != set(expectation_ids)
    ):
        raise SlotExtractionInputError("slot_extraction_catalog_mismatch")
    by_id = {item.case_id: item for item in expectations}
    for case in cases:
        expected = by_id[case.id]
        if case.module != expected.module or case.category != expected.category:
            raise SlotExtractionInputError("slot_extraction_case_contract_mismatch")
        if (
            len(expected.applicable_metrics) != len(set(expected.applicable_metrics))
            or not set(expected.applicable_metrics) <= METRIC_NAMES
        ):
            raise SlotExtractionInputError("invalid_slot_extraction_metrics")
    return cases, by_id


@dataclass(slots=True)
class _MetricAccumulator:
    numerator: int = 0
    denominator: int = 0

    def add(self, passed: int, applicable: int) -> None:
        self.numerator += passed
        self.denominator += applicable

    def to_dict(self) -> dict[str, int | float | None]:
        value = (
            self.numerator / self.denominator
            if self.denominator
            else None
        )
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "value": value,
        }


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _expected_rejected(
    expectation: SlotExtractionExpectation,
) -> set[tuple[str, str]]:
    return {
        (item.slot_name, item.reason_code)
        for item in expectation.expected_rejected
    }


def score_slot_extraction_results(
    cases: list[SlotExtractionCase],
    manifest: dict[str, SlotExtractionExpectation],
    traces: dict[str, SlotExtractionTrace],
) -> dict[str, object]:
    case_ids = {case.id for case in cases}
    if case_ids != set(manifest) or case_ids != set(traces):
        raise SlotExtractionInputError("slot_extraction_result_catalog_mismatch")
    metrics = {name: _MetricAccumulator() for name in METRIC_NAMES}
    extraction_latencies: list[int] = []
    incomplete_latencies: list[int] = []
    total_latencies: list[int] = []
    results: list[dict[str, object]] = []
    write_executed = 0
    created_resources = 0
    duplicate_resources = 0

    for case in cases:
        expected = manifest[case.id]
        trace = traces[case.id]
        applicable = set(expected.applicable_metrics)
        if "envelope_valid" in applicable:
            metrics["envelope_valid"].add(int(trace.envelope_valid), 1)
        if "field_precision" in applicable:
            correct = sum(
                name in expected.expected_fields
                and expected.expected_fields[name] == value
                for name, value in trace.accepted_fields.items()
            )
            metrics["field_precision"].add(correct, len(trace.accepted_fields))
        if "field_recall" in applicable:
            correct = sum(
                trace.accepted_fields.get(name) == value
                for name, value in expected.expected_fields.items()
            )
            metrics["field_recall"].add(correct, len(expected.expected_fields))
        if "ambiguity_handled" in applicable:
            passed = sum(
                trace.pending.get(name) == reason
                and trace.canonical_fields.get(name) == case.initial_draft.get(name)
                for name, reason in expected.expected_pending.items()
            )
            metrics["ambiguity_handled"].add(
                passed, len(expected.expected_pending)
            )
        if "source_verified" in applicable:
            evidence_by_name = {
                item.slot_name: item for item in trace.source_evidence
            }
            metrics["source_verified"].add(
                sum(
                    evidence_by_name.get(name) is not None
                    and evidence_by_name[name].verified
                    for name in trace.accepted_fields
                ),
                len(trace.accepted_fields),
            )
        if "clarification_correct" in applicable:
            expected_fields = set(expected.expected_clarification_fields)
            passed = (
                trace.terminal == "clarification"
                and set(trace.clarification_fields) == expected_fields
            )
            metrics["clarification_correct"].add(int(passed), 1)
        if "terminal_correct" in applicable:
            dispositions_match = (
                trace.accepted_fields == expected.expected_fields
                and trace.pending == expected.expected_pending
                and set(trace.rejected) == _expected_rejected(expected)
            )
            metrics["terminal_correct"].add(
                int(dispositions_match and trace.terminal == expected.expected_terminal),
                1,
            )
        if "must_not_execute" in applicable and expected.must_not_execute:
            safe = (
                trace.write_executed == 0
                and trace.created_resources == 0
                and trace.duplicate_resources == 0
            )
            metrics["must_not_execute"].add(int(safe), 1)
        if "call_budget_respected" in applicable:
            metrics["call_budget_respected"].add(
                int(trace.extraction_calls <= 1), 1
            )

        extraction_latencies.append(trace.extraction_latency_ms)
        if trace.pending or trace.clarification_fields:
            incomplete_latencies.append(trace.incomplete_turn_latency_ms)
        total_latencies.append(trace.total_turn_latency_ms)
        write_executed += trace.write_executed
        created_resources += trace.created_resources
        duplicate_resources += trace.duplicate_resources
        results.append({
            "case_id": case.id,
            "module": case.module,
            "category": case.category,
            "envelope_valid": trace.envelope_valid,
            "accepted_field_names": sorted(trace.accepted_fields),
            "pending": dict(trace.pending),
            "rejected": [
                {"slot_name": name, "reason_code": reason}
                for name, reason in trace.rejected
            ],
            "clarification_fields": list(trace.clarification_fields),
            "terminal": trace.terminal,
            "error_code": trace.error_code,
            "source_evidence": [
                {
                    "slot_name": item.slot_name,
                    "source_turn_id": item.source_turn_id,
                    "match_kind": item.match_kind,
                    "source_spans": [list(span) for span in item.source_spans],
                    "verified": item.verified,
                }
                for item in trace.source_evidence
            ],
            "extraction_calls": trace.extraction_calls,
            "extraction_latency_ms": trace.extraction_latency_ms,
            "incomplete_turn_latency_ms": trace.incomplete_turn_latency_ms,
            "total_turn_latency_ms": trace.total_turn_latency_ms,
            "write_executed": trace.write_executed,
            "created_resources": trace.created_resources,
            "duplicate_resources": trace.duplicate_resources,
        })

    metric_payload: dict[str, object] = {
        name: metrics[name].to_dict() for name in sorted(METRIC_NAMES)
    }
    metric_payload.update({
        "sample_count": len(cases),
        "extraction_latency_p95_ms": _p95(extraction_latencies),
        "incomplete_turn_latency_p95_ms": _p95(incomplete_latencies),
        "total_turn_latency_p95_ms": _p95(total_latencies),
        "write_executed": write_executed,
        "created_resources": created_resources,
        "duplicate_resources": duplicate_resources,
    })
    return {"schema_version": 1, "metrics": metric_payload, "results": results}


def _metric_value(metrics: dict[str, object], name: str) -> float | None:
    item = metrics.get(name)
    if not isinstance(item, dict):
        return None
    value = item.get("value")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def assess_slot_extraction_quality(report: dict[str, object]) -> dict[str, object]:
    raw_metrics = report.get("metrics")
    metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    checks = {
        "envelope_valid": (_metric_value(metrics, "envelope_valid"), ">=", 0.99),
        "field_precision": (_metric_value(metrics, "field_precision"), ">=", 0.98),
        "field_recall": (_metric_value(metrics, "field_recall"), ">=", 0.95),
        "ambiguity_handled": (
            _metric_value(metrics, "ambiguity_handled"), "==", 1.0
        ),
        "source_verified": (_metric_value(metrics, "source_verified"), "==", 1.0),
        "clarification_correct": (
            _metric_value(metrics, "clarification_correct"), "==", 1.0
        ),
        "terminal_correct": (_metric_value(metrics, "terminal_correct"), ">=", 0.95),
        "must_not_execute": (
            _metric_value(metrics, "must_not_execute"), "==", 1.0
        ),
        "call_budget_respected": (
            _metric_value(metrics, "call_budget_respected"), "==", 1.0
        ),
    }
    rendered: dict[str, object] = {}
    passed = True
    for name, (actual, operator, target) in checks.items():
        ok = actual is not None and (
            actual >= target if operator == ">=" else actual == target
        )
        rendered[name] = {
            "actual": actual, "operator": operator, "target": target, "passed": ok,
        }
        passed = passed and ok
    latency_limits = {
        "extraction_latency_p95_ms": 3000,
        "incomplete_turn_latency_p95_ms": 4000,
        "total_turn_latency_p95_ms": 8000,
    }
    for name, target in latency_limits.items():
        actual = metrics.get(name)
        ok = isinstance(actual, int) and not isinstance(actual, bool) and actual <= target
        rendered[name] = {
            "actual": actual, "operator": "<=", "target": target, "passed": ok,
        }
        passed = passed and ok
    for name in ("write_executed", "created_resources", "duplicate_resources"):
        actual = metrics.get(name)
        ok = actual == 0
        rendered[name] = {
            "actual": actual, "operator": "==", "target": 0, "passed": ok,
        }
        passed = passed and ok
    return {"status": "passed" if passed else "failed", "checks": rendered}


__all__ = [
    "PUBLIC_CATEGORY_DISTRIBUTION",
    "RejectedExpectation",
    "SlotExtractionCase",
    "SlotExtractionExpectation",
    "SlotExtractionInputError",
    "SlotExtractionTrace",
    "SourceEvidence",
    "assess_slot_extraction_quality",
    "load_slot_extraction_catalog",
    "score_slot_extraction_results",
]
