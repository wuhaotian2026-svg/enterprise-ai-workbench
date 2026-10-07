from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "evaluate_workbench_foundation.py"
)


def load_evaluator():
    spec = importlib.util.spec_from_file_location(
        "evaluate_workbench_foundation",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("evaluator_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_uses_exact_denominators_and_no_false_perfect_score() -> None:
    evaluator = load_evaluator()
    observations = evaluator.FoundationObservations(
        module_authorization_passes=14,
        module_authorization_cases=14,
        scoped_hr_forbidden_successes=0,
        scoped_hr_forbidden_cases=3,
        product_event_sensitive_hits=0,
        product_event_rows=20,
        security_audit_sensitive_hits=0,
        security_audit_rows=4,
        duplicate_event_resources=0,
        duplicate_event_resource_cases=20,
        zero_denominator_false_perfect_scores=0,
        zero_denominator_cases=0,
        metric_contract_passes=0,
        metric_contract_cases=0,
    )

    report = evaluator.build_report(observations)

    assert report["status"] == "passed"
    assert report["module_authorization_cases"] == {
        "passed": 14,
        "total": 14,
        "rate": 1.0,
    }
    assert report["scoped_hr_forbidden_successes"] == {
        "count": 0,
        "total": 3,
        "rate": 0.0,
    }
    assert report["zero_denominator_false_perfect_scores"] == {
        "count": 0,
        "total": 0,
        "rate": None,
    }
    assert report["metric_contract_cases"] == {
        "passed": 0,
        "total": 0,
        "rate": None,
    }


def test_report_fails_when_any_security_or_contract_observation_fails() -> None:
    evaluator = load_evaluator()
    observations = evaluator.FoundationObservations(
        module_authorization_passes=6,
        module_authorization_cases=7,
        scoped_hr_forbidden_successes=1,
        scoped_hr_forbidden_cases=2,
        product_event_sensitive_hits=1,
        product_event_rows=5,
        security_audit_sensitive_hits=1,
        security_audit_rows=2,
        duplicate_event_resources=1,
        duplicate_event_resource_cases=5,
        zero_denominator_false_perfect_scores=1,
        zero_denominator_cases=1,
        metric_contract_passes=4,
        metric_contract_cases=5,
    )

    report = evaluator.build_report(observations)

    assert report["status"] == "failed"
    assert report["module_authorization_cases"]["rate"] == pytest.approx(6 / 7)
    assert report["scoped_hr_forbidden_successes"]["count"] == 1
    assert report["product_event_sensitive_hits"]["count"] == 1
    assert report["security_audit_sensitive_hits"]["count"] == 1
    assert report["duplicate_event_resources"]["count"] == 1
    assert report["zero_denominator_false_perfect_scores"]["count"] == 1
    assert report["metric_contract_cases"]["rate"] == pytest.approx(4 / 5)


def test_sensitive_scan_counts_rows_once_without_exposing_values() -> None:
    evaluator = load_evaluator()
    rows = [
        ("event-1", {"outcome": "ok", "dimensions": {"code": "safe"}}),
        (
            "event-2",
            {
                "outcome": "contains employee private reason",
                "dimensions": {"error_code": "secret-marker"},
            },
        ),
    ]

    hits = evaluator.scan_sensitive_rows(
        rows,
        sensitive_values=("employee private reason", "secret"),
    )

    assert hits == ["event-2"]
    assert "employee private reason" not in json.dumps(hits)


def test_database_target_rejects_non_test_urls() -> None:
    evaluator = load_evaluator()

    evaluator.validate_database_target(
        "postgresql+psycopg://u:p@localhost/workbench_quality_test"
    )
    with pytest.raises(evaluator.FoundationEvaluationError, match="test_database_required"):
        evaluator.validate_database_target(
            "postgresql+psycopg://u:p@localhost/workbench_production"
        )


def test_atomic_report_replaces_complete_json_without_temp_residue(
    tmp_path: Path,
) -> None:
    evaluator = load_evaluator()
    output = tmp_path / "foundation-report.json"
    output.write_text('{"stale": true}', encoding="utf-8")

    evaluator.write_report_atomic(output, {"status": "passed", "version": "v1"})

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "status": "passed",
        "version": "v1",
    }
    assert list(tmp_path.iterdir()) == [output]
