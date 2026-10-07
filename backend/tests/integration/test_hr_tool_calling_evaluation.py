from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from policy_api.evaluation.tool_calling import (
    ToolCallingEvaluationRunner,
    load_tool_calling_cases,
)


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evaluation" / "hr_tool_calling_cases.json"
REQUIRED_FIELDS = {
    "id",
    "split",
    "category",
    "input_turns",
    "expected_tool",
    "expected_arguments",
    "parameter_expectation",
    "expected_clarification",
    "expected_error",
    "allow_write_proposal",
    "must_not_execute",
    "allowed_created_resource_count",
    "tags",
}
REQUIRED_CATEGORIES = {
    "direct_balance",
    "paraphrase",
    "complete_leave",
    "missing_fields",
    "relative_cross_week",
    "invalid_dates",
    "calendar_edge",
    "insufficient_balance",
    "overlap",
    "status_query",
    "pending_cancel",
    "terminal_reoperation",
    "foreign_resource",
    "actor_field_injection",
    "prompt_injection",
    "idempotency_conflict",
    "confirmation_expiry",
    "provider_protocol",
    "rag_insufficient_business_available",
    "provider_failure",
}


def test_fixed_dataset_has_exact_split_contract_and_required_categories() -> None:
    raw = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = load_tool_calling_cases(DATASET)

    assert raw == cases
    assert len(cases) == 60
    assert [case["id"] for case in cases] == [f"HR-TC-{index:03d}" for index in range(1, 61)]
    assert len({case["id"] for case in cases}) == 60
    assert all(REQUIRED_FIELDS <= set(case) for case in cases)
    assert Counter(case["split"] for case in cases) == {"development": 40, "holdout": 20}
    assert REQUIRED_CATEGORIES <= {case["category"] for case in cases}
    assert all(case["parameter_expectation"] in {"complete", "clarification", "not_applicable"} for case in cases)
    assert all(isinstance(case["input_turns"], list) and case["input_turns"] for case in cases)
    assert all("employee_id" not in case["expected_arguments"] for case in cases)
    assert all(case["allowed_created_resource_count"] in {0, 1} for case in cases)


def test_deterministic_fixture_runner_reports_separate_perfect_metrics(tmp_path: Path) -> None:
    cases = load_tool_calling_cases(DATASET)

    def evaluate(case: dict[str, object]) -> dict[str, object]:
        expected_tool = case["expected_tool"]
        expected_clarification = case["expected_clarification"]
        return {
            "selected_tool": expected_tool,
            "normalized_arguments": case["expected_arguments"],
            "clarification_fields": expected_clarification,
            "error_code": case["expected_error"],
            "write_proposed": bool(case["allow_write_proposal"]),
            "write_executed": False,
            "created_resource_ids": [],
            "latency_ms": 5,
        }

    output = tmp_path / "deterministic-tool-calling.json"
    report = ToolCallingEvaluationRunner(
        evaluate=evaluate,
        configuration={"mode": "deterministic_fixture", "api_key": "must-not-leak"},
    ).run(cases, output)

    assert len(report["results"]) == 60
    assert report["configuration"] == {"mode": "deterministic_fixture"}
    assert report["case_set_sha256"]
    assert report["metrics"]["tool_selection_accuracy"]["value"] == 1.0
    assert report["metrics"]["complete_parameter_accuracy"]["value"] == 1.0
    assert report["metrics"]["clarification_accuracy"]["value"] == 1.0
    assert report["metrics"]["must_not_execute_accuracy"]["value"] == 1.0
    assert report["metrics"]["duplicate_resource_count"] == 0
    assert "must-not-leak" not in output.read_text(encoding="utf-8")


def test_runner_resumes_matching_partial_report_without_repeating_cases(tmp_path: Path) -> None:
    cases = load_tool_calling_cases(DATASET)[:2]
    evaluated_ids: list[str] = []
    fail_second_once = True

    def evaluate(case: dict[str, object]) -> dict[str, object]:
        nonlocal fail_second_once
        evaluated_ids.append(str(case["id"]))
        if case["id"] == "HR-TC-002" and fail_second_once:
            fail_second_once = False
            raise RuntimeError("simulated_interruption")
        return {
            "selected_tool": case["expected_tool"],
            "normalized_arguments": case["expected_arguments"],
            "clarification_fields": case["expected_clarification"],
            "error_code": case["expected_error"],
            "write_proposed": bool(case["allow_write_proposal"]),
            "write_executed": False,
            "created_resource_ids": [],
            "latency_ms": 7,
        }

    output = tmp_path / "resumable-tool-calling.json"
    runner = ToolCallingEvaluationRunner(
        evaluate=evaluate,
        configuration={"mode": "deterministic_fixture", "nested": {"token": "hidden"}},
    )
    with pytest.raises(RuntimeError, match="simulated_interruption"):
        runner.run(cases, output)
    report = runner.run(cases, output)

    assert evaluated_ids == ["HR-TC-001", "HR-TC-002", "HR-TC-002"]
    assert [result["id"] for result in report["results"]] == ["HR-TC-001", "HR-TC-002"]
    assert report["configuration"] == {
        "mode": "deterministic_fixture",
        "nested": {},
    }
    assert "hidden" not in output.read_text(encoding="utf-8")
