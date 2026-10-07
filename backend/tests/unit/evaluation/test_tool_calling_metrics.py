from __future__ import annotations

import pytest

from policy_api.evaluation.tool_calling import (
    ToolCallingEvaluationInputError,
    compute_tool_calling_metrics,
)


def test_tool_calling_metrics_report_exact_denominators_duplicates_and_p95() -> None:
    results = [
        {
            "tool_selection_passed": True,
            "complete_parameters_passed": True,
            "clarification_passed": None,
            "must_not_execute_passed": True,
            "created_resource_ids": ["request-a"],
            "allowed_created_resource_count": 1,
            "latency_ms": 100,
        },
        {
            "tool_selection_passed": True,
            "complete_parameters_passed": False,
            "clarification_passed": None,
            "must_not_execute_passed": None,
            "created_resource_ids": ["request-b", "request-c"],
            "allowed_created_resource_count": 1,
            "latency_ms": 200,
        },
        {
            "tool_selection_passed": False,
            "complete_parameters_passed": None,
            "clarification_passed": True,
            "must_not_execute_passed": True,
            "created_resource_ids": [],
            "allowed_created_resource_count": 0,
            "latency_ms": 300,
        },
        {
            "tool_selection_passed": True,
            "complete_parameters_passed": None,
            "clarification_passed": False,
            "must_not_execute_passed": None,
            "created_resource_ids": ["unexpected-request"],
            "allowed_created_resource_count": 0,
            "latency_ms": 400,
        },
    ]

    assert compute_tool_calling_metrics(results) == {
        "sample_count": 4,
        "tool_selection_accuracy": {"numerator": 3, "denominator": 4, "value": 0.75},
        "complete_parameter_accuracy": {"numerator": 1, "denominator": 2, "value": 0.5},
        "clarification_accuracy": {"numerator": 1, "denominator": 2, "value": 0.5},
        "must_not_execute_accuracy": {"numerator": 2, "denominator": 2, "value": 1.0},
        "duplicate_resource_count": 2,
        "p95_latency_ms": 400,
    }


def test_zero_denominator_metrics_are_unavailable_not_perfect() -> None:
    metrics = compute_tool_calling_metrics([
        {
            "tool_selection_passed": True,
            "complete_parameters_passed": None,
            "clarification_passed": None,
            "must_not_execute_passed": None,
            "created_resource_ids": [],
            "allowed_created_resource_count": 0,
            "latency_ms": 10,
        }
    ])

    assert metrics["complete_parameter_accuracy"] == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }
    assert metrics["clarification_accuracy"]["value"] is None
    assert metrics["must_not_execute_accuracy"]["value"] is None


def test_tool_calling_metrics_reject_empty_results() -> None:
    with pytest.raises(ToolCallingEvaluationInputError, match="empty_results"):
        compute_tool_calling_metrics([])
