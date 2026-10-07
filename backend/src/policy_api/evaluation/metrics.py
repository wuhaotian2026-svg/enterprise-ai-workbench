from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any


class EvaluationInputError(ValueError):
    pass


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def compute_metrics(results: Sequence[dict[str, Any]]) -> dict[str, int | float]:
    if not results:
        raise EvaluationInputError("empty_results")
    answered = [item for item in results if item["expected_status"] == "answered"]
    abstentions = [item for item in results if item["expected_status"] == "abstained"]
    correct = sum(item["actual_status"] == item["expected_status"] and bool(item["facts_passed"]) for item in results)
    citation_supported = sum(bool(item.get("citations_passed")) for item in answered)
    correct_abstentions = sum(item["actual_status"] == "abstained" for item in abstentions)
    latencies = sorted(int(item["latency_ms"]) for item in results)
    p95 = latencies[max(0, math.ceil(len(latencies) * .95) - 1)]
    reviewed = sum(bool(item.get("human_reviewed")) for item in results)
    return {
        "sample_count": len(results),
        "answer_correctness": _ratio(correct, len(results)),
        "citation_support": _ratio(citation_supported, len(answered)),
        "correct_abstention": _ratio(correct_abstentions, len(abstentions)),
        "p95_latency_ms": p95,
        "human_reviewed_count": reviewed,
        "unreviewed_count": len(results) - reviewed,
    }


def _applicable_metric(values: Sequence[bool | None]) -> dict[str, int | float | None]:
    applicable = [value for value in values if value is not None]
    numerator = sum(value is True for value in applicable)
    denominator = len(applicable)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(numerator / denominator, 4) if denominator else None,
    }


def _violation_metric(values: Sequence[bool]) -> dict[str, int | float | None]:
    numerator = sum(value is True for value in values)
    denominator = len(values)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(numerator / denominator, 4) if denominator else None,
    }


def _p95_metric(values: Sequence[int]) -> dict[str, int | None]:
    ordered = sorted(values)
    value = (
        ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]
        if ordered
        else None
    )
    return {"numerator": value or 0, "denominator": len(ordered), "value": value}


def compute_rag_v2_metrics(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "retrieval_recall_at_5": _applicable_metric(
            [item.get("retrieval_recall_at_5_passed") for item in results]
        ),
        "specific_terminal_correctness": _applicable_metric(
            [item.get("specific_terminal_correctness_passed") for item in results]
        ),
        "answer_correctness": _applicable_metric(
            [item.get("answer_correctness_passed") for item in results]
        ),
        "clarification_precision": _applicable_metric(
            [item.get("clarification_precision_passed") for item in results]
        ),
        "clarification_recall": _applicable_metric(
            [item.get("clarification_recall_passed") for item in results]
        ),
        "citation_support": _applicable_metric(
            [item.get("citation_support_passed") for item in results]
        ),
        "citation_validation": _applicable_metric(
            [item.get("citation_validation_passed") for item in results]
        ),
        "correct_abstention": _applicable_metric(
            [item.get("correct_abstention_passed") for item in results]
        ),
        "disabled_retrieval_violations": _violation_metric(
            [bool(item.get("disabled_retrieval_violation")) for item in results]
        ),
        "unsupported_definitive_answers": _violation_metric(
            [bool(item.get("unsupported_definitive_answer")) for item in results]
        ),
        "no_evidence_conflict_must_abstain": _applicable_metric(
            [
                item.get("no_evidence_conflict_must_abstain_passed")
                for item in results
            ]
        ),
        "retrieval_p95_ms": _p95_metric(
            [int(item["retrieval_latency_ms"]) for item in results]
        ),
        "end_to_end_p95_ms": _p95_metric(
            [int(item["end_to_end_latency_ms"]) for item in results]
        ),
    }
