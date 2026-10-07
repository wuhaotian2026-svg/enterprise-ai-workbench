from __future__ import annotations

from pathlib import Path

from policy_api.evaluation.tool_calling import (
    ToolCallingEvaluationRunner,
    load_tool_calling_cases,
)


ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "evaluation" / "hr_tool_calling_cases.json"


def test_report_separates_development_and_holdout_metrics(tmp_path: Path) -> None:
    cases = load_tool_calling_cases(DATASET)

    def evaluate(case: dict[str, object]) -> dict[str, object]:
        return {
            "selected_tool": case["expected_tool"],
            "normalized_arguments": case["expected_arguments"],
            "clarification_fields": case["expected_clarification"],
            "error_code": case["expected_error"],
            "write_proposed": bool(case["allow_write_proposal"]),
            "write_executed": False,
            "created_resource_ids": [],
            "latency_ms": 5,
        }

    report = ToolCallingEvaluationRunner(
        evaluate=evaluate,
        configuration={"mode": "deterministic_fixture"},
    ).run(cases, tmp_path / "report.json")

    assert report["metrics"]["sample_count"] == 60
    assert report["metrics_by_split"]["development"]["sample_count"] == 40
    assert report["metrics_by_split"]["holdout"]["sample_count"] == 20
    assert report["metrics_by_split"]["development"]["tool_selection_accuracy"]["value"] == 1.0
    assert report["metrics_by_split"]["holdout"]["tool_selection_accuracy"]["value"] == 1.0
