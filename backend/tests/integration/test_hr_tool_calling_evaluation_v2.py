from __future__ import annotations

import json
from pathlib import Path

from policy_api.evaluation.real_tool_flow import (
    EvaluationFixtureStore,
    build_safe_evaluation_registry,
)
from policy_api.evaluation.tool_calling import load_tool_calling_cases
from policy_api.evaluation.tool_calling_v2 import (
    V2EvaluationRunner,
    V2FlowTrace,
    V2PlannedCall,
    load_v2_manifest,
)


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evaluation" / "hr_tool_calling_cases.json"
MANIFEST = ROOT / "evaluation" / "hr_tool_calling_case_manifest_v2.json"


def test_v2_runner_accounts_for_all_60_but_calls_model_layer_only(
    tmp_path: Path,
) -> None:
    cases = load_tool_calling_cases(DATASET)
    registry = build_safe_evaluation_registry(EvaluationFixtureStore())
    manifest = load_v2_manifest(MANIFEST, cases=cases, registry=registry)
    evaluated_ids: list[str] = []

    def evaluate(case, contract):  # type: ignore[no-untyped-def]
        evaluated_ids.append(case["id"])
        calls = tuple(
            V2PlannedCall(
                tool_name,
                case["expected_arguments"] if tool_name == contract.target_tool else {},
            )
            for tool_name in contract.required_tools
        )
        final_text = None
        if contract.expected_terminal == "clarification":
            final_text = "请补充请假类型、开始日期、结束日期和请假原因。"
        elif contract.expected_terminal in {"text", "safe_refusal"}:
            final_text = "无法越权操作。" if contract.expected_terminal == "safe_refusal" else "查询完成。"
        return V2FlowTrace(
            planned_calls=calls,
            final_text=final_text,
            terminal_kind=contract.expected_terminal,
            terminal_error=contract.expected_error,
            model_call_count=1,
            read_call_count=sum("submit" not in item.tool_name and "cancel" not in item.tool_name for item in calls),
            write_proposal_count=1 if contract.expected_terminal == "write_proposal" else 0,
            latency_ms=5,
            write_executed=False,
            created_resource_ids=(),
        )

    output = tmp_path / "v2.json"
    report = V2EvaluationRunner(
        evaluate=evaluate,
        registry=registry,
        manifest=manifest,
        configuration={"mode": "fixture-contract"},
        fingerprint={"test": "v2"},
        force_fresh=True,
    ).run(cases, output)

    accounted = [item["id"] for item in report["results"]] + [
        item["id"] for item in report["excluded_cases"]
    ]
    assert len(evaluated_ids) == 48
    assert len(set(evaluated_ids)) == 48
    assert len(accounted) == len(set(accounted)) == 60
    assert report["coverage"] == {
        "catalog_total": 60,
        "real_model_total": 48,
        "deterministic_contract_total": 12,
        "development_real_model_total": 40,
        "holdout_real_model_total": 8,
    }
    assert report["metrics"]["sample_count"] == 48
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 2


def test_excluded_cases_map_to_production_codes_and_real_test_categories() -> None:
    cases = load_tool_calling_cases(DATASET)
    registry = build_safe_evaluation_registry(EvaluationFixtureStore())
    manifest = load_v2_manifest(MANIFEST, cases=cases, registry=registry)
    expected = {
        "HR-TC-046": ("operation_id_conflict", ("leave_transaction_idempotency",)),
        "HR-TC-047": ("operation_id_conflict", ("leave_transaction_idempotency",)),
        "HR-TC-048": (None, ("leave_transaction_idempotency",)),
        "HR-TC-049": ("confirmation_expired", ("tool_confirmation_lifecycle",)),
        "HR-TC-050": ("confirmation_already_used", ("tool_confirmation_lifecycle",)),
        "HR-TC-051": ("confirmation_arguments_mismatch", ("tool_confirmation_lifecycle",)),
        "HR-TC-052": ("unknown_tool", ("tool_registry_protocol",)),
        "HR-TC-053": ("tool_write_limit_exceeded", ("bounded_orchestrator_limits",)),
        "HR-TC-054": ("tool_model_call_limit_exceeded", ("bounded_orchestrator_limits",)),
        "HR-TC-058": ("tool_provider_timeout", ("planner_provider_failures",)),
        "HR-TC-059": ("tool_provider_unavailable", ("planner_provider_failures",)),
        "HR-TC-060": ("tool_provider_rate_limited", ("planner_provider_failures",)),
    }

    actual = {
        case_id: (contract.expected_error, contract.evidence)
        for case_id, contract in manifest.items()
        if contract.layer == "deterministic_contract"
    }

    assert actual == expected
    assert not {
        "tool_confirmation_expired",
        "tool_confirmation_consumed",
        "tool_confirmation_mismatch",
        "provider_timeout",
        "provider_unavailable",
        "provider_rate_limited",
        "tool_loop_limit_exceeded",
        "multiple_write_tools_forbidden",
    } & {code for code, _evidence in actual.values() if code is not None}


def test_v2_runner_split_never_evaluates_the_other_model_population(
    tmp_path: Path,
) -> None:
    cases = load_tool_calling_cases(DATASET)
    registry = build_safe_evaluation_registry(EvaluationFixtureStore())
    manifest = load_v2_manifest(MANIFEST, cases=cases, registry=registry)

    for split, expected_count in (("development", 40), ("holdout", 8)):
        evaluated_ids: list[str] = []

        def evaluate(case, contract):  # type: ignore[no-untyped-def]
            evaluated_ids.append(case["id"])
            final_text = "查询完成。"
            terminal = contract.expected_terminal
            if terminal == "clarification":
                final_text = "请补充请假类型、开始日期、结束日期和请假原因。"
            elif terminal == "safe_refusal":
                final_text = "无法越权操作。"
            return V2FlowTrace(
                planned_calls=tuple(
                    V2PlannedCall(
                        name,
                        case["expected_arguments"]
                        if name == contract.target_tool
                        else {},
                    )
                    for name in contract.required_tools
                ),
                final_text=(
                    None if terminal in {"write_proposal", "error"} else final_text
                ),
                terminal_kind=terminal,
                terminal_error=contract.expected_error,
                model_call_count=1,
                read_call_count=0,
                write_proposal_count=1 if terminal == "write_proposal" else 0,
                latency_ms=5,
                write_executed=False,
                created_resource_ids=(),
            )

        report = V2EvaluationRunner(
            evaluate=evaluate,
            registry=registry,
            manifest=manifest,
            configuration={"mode": "fixture-contract", "split": split},
            fingerprint={"test": "v2"},
            force_fresh=True,
            split=split,
        ).run(cases, tmp_path / f"{split}.json")

        assert report["status"] == "complete"
        assert report["metrics"]["sample_count"] == expected_count
        assert report["run_scope"] == {
            "selected_split": split,
            "selected_real_model_total": expected_count,
            "selected_development_real_model_total": (
                40 if split == "development" else 0
            ),
            "selected_holdout_real_model_total": 8 if split == "holdout" else 0,
        }
        assert len(evaluated_ids) == expected_count
        assert all(
            next(case for case in cases if case["id"] == case_id)["split"] == split
            for case_id in evaluated_ids
        )
        assert all(result["split"] == split for result in report["results"])
