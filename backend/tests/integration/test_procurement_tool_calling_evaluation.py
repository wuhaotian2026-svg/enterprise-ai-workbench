from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from policy_api.evaluation.procurement_tool_calling import (
    ProcurementEvaluationRunner,
    assess_procurement_quality,
)
from policy_api.evaluation.procurement_holdout_generator import (
    generate_procurement_holdout,
)
from policy_api.evaluation.real_procurement_tool_flow import (
    ProcurementEvaluationFixtureStore,
    build_safe_procurement_evaluation_registry,
)
from policy_api.evaluation.tool_calling import (
    ToolCallingEvaluationInputError,
    load_tool_calling_cases,
)
from policy_api.evaluation.tool_calling_v2 import V2FlowTrace, V2PlannedCall, load_v2_manifest


ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = ROOT.parent
DATASET = ROOT / "evaluation" / "procurement_tool_calling_cases.json"
MANIFEST = ROOT / "evaluation" / "procurement_tool_calling_case_manifest_v1.json"


def test_public_catalog_runs_deterministically_with_zero_write_and_resource_counts(tmp_path: Path) -> None:
    cases = load_tool_calling_cases(DATASET)
    registry = build_safe_procurement_evaluation_registry(ProcurementEvaluationFixtureStore())
    manifest = load_v2_manifest(MANIFEST, cases=cases, registry=registry)

    def evaluate(case, contract):  # type: ignore[no-untyped-def]
        calls = tuple(
            V2PlannedCall(name, case["expected_arguments"] if name == contract.target_tool else {})
            for name in contract.required_tools
        )
        terminal = contract.expected_terminal
        final_text = None
        if terminal == "clarification":
            aliases = {
                "title": "标题", "purpose": "用途",
                "needed_by_date": "需要日期", "currency": "币种",
                "items": "采购明细", "reason": "拒绝理由",
            }
            final_text = "请补充" + "、".join(aliases[field] for field in case["expected_clarification"])
        elif terminal in {"text", "safe_refusal"}:
            final_text = "无法越权或批量执行。" if terminal == "safe_refusal" else "查询完成。"
        return V2FlowTrace(
            planned_calls=calls, final_text=final_text, terminal_kind=terminal,
            terminal_error=contract.expected_error, model_call_count=1,
            read_call_count=sum(
                "submit" not in call.tool_name and "withdraw" not in call.tool_name
                and "approve" not in call.tool_name and "reject" not in call.tool_name
                for call in calls
            ),
            write_proposal_count=1 if terminal == "write_proposal" else 0,
            latency_ms=5, write_executed=False, created_resource_ids=(),
        )

    report = ProcurementEvaluationRunner(
        evaluate=evaluate, registry=registry, manifest=manifest,
        configuration={"mode": "deterministic-contract"},
        fingerprint={"test": "procurement"}, force_fresh=True,
    ).run(cases, tmp_path / "report.json")
    quality = assess_procurement_quality(report)

    assert len(report["results"]) == len(cases)
    assert all(result["write_executed"] is False for result in report["results"])
    assert all(result["created_resource_ids"] == [] for result in report["results"])
    assert report["metrics"]["duplicate_resource_count"] == 0
    assert quality["safety"]["write_executed_count"] == 0
    assert quality["safety"]["created_resource_count"] == 0
    assert quality["safety"]["duplicate_resource_count"] == 0
    assert report["metrics"]["complete_parameter_accuracy"] == {
        "numerator": 15,
        "denominator": 15,
        "value": 1.0,
    }
    assert quality["status"] == "passed"

    resumed_evaluations: list[str] = []

    def must_resume(case, contract):  # type: ignore[no-untyped-def]
        resumed_evaluations.append(case["id"])
        raise AssertionError((case, contract))

    resumed = ProcurementEvaluationRunner(
        evaluate=must_resume, registry=registry, manifest=manifest,
        configuration={"mode": "deterministic-contract"},
        fingerprint={"test": "procurement"}, force_fresh=False,
    ).run(cases, tmp_path / "report.json")
    assert resumed_evaluations == []
    assert resumed["results"] == report["results"]


def test_one_error_terminal_bypass_fails_the_twenty_case_holdout_quality_gate(
    tmp_path: Path,
) -> None:
    cases, manifest_payload = generate_procurement_holdout(
        "integration-error-contract", count=20
    )
    registry = build_safe_procurement_evaluation_registry(
        ProcurementEvaluationFixtureStore()
    )
    manifest_file = tmp_path / "holdout-manifest.json"
    manifest_file.write_text(
        json.dumps(manifest_payload, ensure_ascii=False), encoding="utf-8"
    )
    manifest = load_v2_manifest(manifest_file, cases=cases, registry=registry)
    bypass_id = next(
        case_id
        for case_id, contract in manifest.items()
        if contract.expected_terminal == "error"
    )

    def evaluate(case, contract):  # type: ignore[no-untyped-def]
        calls = tuple(
            V2PlannedCall(
                name,
                case["expected_arguments"] if name == contract.target_tool else {},
            )
            for name in contract.required_tools
        )
        if case["id"] == bypass_id:
            return V2FlowTrace(
                planned_calls=calls,
                final_text="我不能处理。",
                terminal_kind="safe_refusal",
                terminal_error=None,
                model_call_count=1,
                read_call_count=0,
                write_proposal_count=0,
                latency_ms=5,
                write_executed=False,
                created_resource_ids=(),
            )
        terminal = contract.expected_terminal
        final_text = None
        if terminal == "clarification":
            aliases = {
                "title": "标题", "purpose": "用途",
                "needed_by_date": "需要日期", "currency": "币种",
                "items": "采购明细", "reason": "拒绝理由",
            }
            final_text = "请补充" + "、".join(
                aliases[field] for field in case["expected_clarification"]
            )
        elif terminal in {"text", "safe_refusal"}:
            final_text = (
                "无法越权或批量执行。"
                if terminal == "safe_refusal"
                else "查询完成。"
            )
        return V2FlowTrace(
            planned_calls=calls,
            final_text=final_text,
            terminal_kind=terminal,
            terminal_error=contract.expected_error,
            model_call_count=1,
            read_call_count=0,
            write_proposal_count=1 if terminal == "write_proposal" else 0,
            latency_ms=5,
            write_executed=False,
            created_resource_ids=(),
        )

    report = ProcurementEvaluationRunner(
        evaluate=evaluate,
        registry=registry,
        manifest=manifest,
        configuration={"mode": "twenty-case-error-contract"},
        fingerprint={"test": "error-contract"},
        force_fresh=True,
        split="holdout",
    ).run(cases, tmp_path / "holdout-report.json")
    quality = assess_procurement_quality(report)
    bypass = next(result for result in report["results"] if result["id"] == bypass_id)

    assert len(report["results"]) == 20
    assert report["metrics"]["tool_selection_accuracy"]["value"] == 1.0
    assert report["metrics"]["complete_parameter_accuracy"]["value"] == 1.0
    assert report["metrics"]["terminal_outcome_accuracy"]["value"] == 0.95
    assert bypass["terminal_outcome_passed"] is False
    assert quality["checks"]["error_terminal_contracts"]["passed"] is False
    assert quality["status"] == "failed"


def test_force_fresh_refuses_to_overwrite_existing_report(tmp_path: Path) -> None:
    cases = load_tool_calling_cases(DATASET)
    registry = build_safe_procurement_evaluation_registry(
        ProcurementEvaluationFixtureStore()
    )
    manifest = load_v2_manifest(MANIFEST, cases=cases, registry=registry)
    output = tmp_path / "historical-report.json"
    original = b'{"historical":"must-survive"}\n'
    output.write_bytes(original)
    evaluated: list[str] = []

    def evaluate(case, contract):  # type: ignore[no-untyped-def]
        evaluated.append(case["id"])
        raise AssertionError(contract)

    runner = ProcurementEvaluationRunner(
        evaluate=evaluate, registry=registry, manifest=manifest,
        configuration={"mode": "deterministic-contract"},
        fingerprint={"test": "procurement"}, force_fresh=True,
    )

    with pytest.raises(
        ToolCallingEvaluationInputError,
        match="procurement_output_exists",
    ):
        runner.run(cases, output)

    assert evaluated == []
    assert output.read_bytes() == original


def test_cli_generates_exact_seed_blind_bundle_without_model_configuration(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    manifest = tmp_path / "manifest.json"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "scripts" / "run_evaluation.py"),
            "--generate-procurement-holdout", "--seed", "integration-seed",
            "--count", "20", "--cases-output", str(cases),
            "--manifest-output", str(manifest),
        ],
        cwd=REPO_ROOT, env=environment, capture_output=True, text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert len(json.loads(cases.read_text(encoding="utf-8"))) == 20
    assert len(json.loads(manifest.read_text(encoding="utf-8"))) == 20
    assert "dataset_sha256=" in completed.stdout
    assert "manifest_sha256=" in completed.stdout
