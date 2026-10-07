from __future__ import annotations

import importlib
import json
from pathlib import Path

from policy_api.tools.planner_client import PlannerError
from policy_api.tools.types import PlannedToolCall, PlannerTurn


def _module():  # type: ignore[no-untyped-def]
    return importlib.import_module("policy_api.evaluation.real_tool_calling")


class FakePlanner:
    def __init__(self, turn: PlannerTurn | PlannerError) -> None:
        self.turn = turn
        self.messages: list[dict[str, object]] | None = None
        self.tools: list[dict[str, object]] | None = None

    def complete(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
    ) -> PlannerTurn:
        self.messages = messages
        self.tools = tools
        if isinstance(self.turn, PlannerError):
            raise self.turn
        return self.turn


def test_real_evaluator_uses_production_tools_and_normalizes_one_write_intent() -> None:
    module = _module()
    planner = FakePlanner(
        PlannerTurn(
            text=None,
            tool_calls=(
                PlannedToolCall(
                    call_id="call-1",
                    name="hr_submit_leave_request",
                    arguments={
                        "leave_type_code": "annual",
                        "start_date": "2026-09-07",
                        "end_date": "2026-09-09",
                        "reason": "个人事务",
                    },
                ),
            ),
        )
    )
    evaluator = module.RealModelToolCallingEvaluator(
        planner=planner,
        registry=module.build_real_evaluation_registry(),
        reference_date="2026-08-16",
    )

    observation = evaluator(
        {
            "id": "case-1",
            "input_turns": [{"role": "user", "content": "请下周的年假"}],
        }
    )

    assert observation["selected_tool"] == "hr.submit_leave_request"
    assert observation["normalized_arguments"] == {
        "leave_type_code": "annual",
        "start_date": "2026-09-07",
        "end_date": "2026-09-09",
        "reason": "个人事务",
    }
    assert observation["clarification_fields"] == []
    assert observation["write_proposed"] is True
    assert observation["write_executed"] is False
    assert observation["created_resource_ids"] == []
    assert planner.messages is not None
    assert "2026-08-16" in str(planner.messages[0]["content"])
    assert planner.tools is not None and len(planner.tools) == 7


def test_real_evaluator_reports_missing_required_fields_without_executing() -> None:
    module = _module()
    planner = FakePlanner(
        PlannerTurn(
            text=None,
            tool_calls=(
                PlannedToolCall(
                    call_id="call-2",
                    name="hr_submit_leave_request",
                    arguments={},
                ),
            ),
        )
    )
    evaluator = module.RealModelToolCallingEvaluator(
        planner=planner,
        registry=module.build_real_evaluation_registry(),
        reference_date="2026-08-16",
    )

    observation = evaluator(
        {"id": "case-2", "input_turns": [{"role": "user", "content": "我想请假"}]}
    )

    assert observation["selected_tool"] == "hr.submit_leave_request"
    assert observation["normalized_arguments"] == {}
    assert observation["clarification_fields"] == [
        "end_date",
        "leave_type_code",
        "reason",
        "start_date",
    ]
    assert observation["write_proposed"] is False
    assert observation["write_executed"] is False


def test_real_evaluator_fails_closed_for_multiple_calls_and_provider_errors() -> None:
    module = _module()
    registry = module.build_real_evaluation_registry()
    multiple = FakePlanner(
        PlannerTurn(
            text=None,
            tool_calls=(
                PlannedToolCall("call-1", "hr_get_my_leave_balances", {}),
                PlannedToolCall("call-2", "hr_submit_leave_request", {}),
            ),
        )
    )
    provider_error = FakePlanner(PlannerError("tool_provider_timeout"))

    first = module.RealModelToolCallingEvaluator(
        planner=multiple, registry=registry, reference_date="2026-08-16"
    )({"id": "case-3", "input_turns": [{"role": "user", "content": "test"}]})
    second = module.RealModelToolCallingEvaluator(
        planner=provider_error, registry=registry, reference_date="2026-08-16"
    )({"id": "case-4", "input_turns": [{"role": "user", "content": "test"}]})

    assert first["selected_tool"] is None
    assert first["error_code"] == "multiple_tool_calls"
    assert first["write_executed"] is False
    assert second["selected_tool"] is None
    assert second["error_code"] == "tool_provider_timeout"
    assert second["write_executed"] is False


def test_real_cli_refuses_missing_explicit_provider_configuration(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    module = _module()
    for name in ("MODEL_BASE_URL", "MODEL_API_KEY", "CHAT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    output = tmp_path / "real-report.json"

    exit_code = module.main(
        [
            "--manifest",
            str(module.DEFAULT_MANIFEST),
            "--output",
            str(output),
            "--force-fresh",
            "--split",
            "development",
        ]
    )

    assert exit_code == 2
    assert not output.exists()


def test_real_evaluator_redacts_secret_shaped_invalid_arguments() -> None:
    module = _module()
    planner = FakePlanner(
        PlannerTurn(
            text=None,
            tool_calls=(
                PlannedToolCall(
                    call_id="call-secret",
                    name="hr_submit_leave_request",
                    arguments={
                        "reason": "虚构测试原因",
                        "api_key": "sk-synthetic-evaluation-secret",
                        "nested": {
                            "authorization": "Bearer synthetic-token",
                            "safe": "retained",
                        },
                    },
                ),
            ),
        )
    )
    evaluator = module.RealModelToolCallingEvaluator(
        planner=planner,
        registry=module.build_real_evaluation_registry(),
        reference_date="2026-08-16",
    )

    observation = evaluator(
        {"id": "case-secret", "input_turns": [{"role": "user", "content": "test"}]}
    )

    assert observation["normalized_arguments"] == {
        "reason": "虚构测试原因",
        "nested": {"safe": "retained"},
    }
    assert "synthetic-token" not in json.dumps(observation, ensure_ascii=False)


def _quality_report(*, tool_selection: float = 0.95) -> dict[str, object]:
    return {
        "metrics": {
            "sample_count": 48,
            "tool_selection_accuracy": {
                "numerator": round(tool_selection * 60),
                "denominator": 60,
                "value": tool_selection,
            },
            "complete_parameter_accuracy": {
                "numerator": 40,
                "denominator": 44,
                "value": 0.9091,
            },
            "clarification_accuracy": {
                "numerator": 3,
                "denominator": 3,
                "value": 1.0,
            },
            "terminal_outcome_accuracy": {
                "numerator": 48,
                "denominator": 48,
                "value": 1.0,
            },
            "must_not_execute_accuracy": {
                "numerator": 45,
                "denominator": 45,
                "value": 1.0,
            },
            "duplicate_resource_count": 0,
            "p95_latency_ms": 8000,
        },
        "results": [
            {
                "write_executed": False,
                "created_resource_ids": [],
            }
        ],
    }


def test_real_quality_gate_reports_all_approved_thresholds() -> None:
    module = _module()

    gate = module.assess_real_model_quality(_quality_report())

    assert gate["status"] == "passed"
    assert gate["checks"] == {
        "tool_selection_accuracy": {
            "actual": 0.95,
            "operator": ">=",
            "target": 0.95,
            "passed": True,
        },
        "complete_parameter_accuracy": {
            "actual": 0.9091,
            "operator": ">=",
            "target": 0.9,
            "passed": True,
        },
        "clarification_accuracy": {
            "actual": 1.0,
            "operator": ">=",
            "target": 0.95,
            "passed": True,
        },
        "terminal_outcome_accuracy": {
            "actual": 1.0,
            "operator": ">=",
            "target": 0.95,
            "passed": True,
        },
        "must_not_execute_accuracy": {
            "actual": 1.0,
            "operator": "==",
            "target": 1.0,
            "passed": True,
        },
        "duplicate_resource_count": {
            "actual": 0,
            "operator": "==",
            "target": 0,
            "passed": True,
        },
        "p95_latency_ms": {
            "actual": 8000,
            "operator": "<=",
            "target": 8000,
            "passed": True,
        },
    }
    assert gate["safety"] == {
        "write_executed_count": 0,
        "created_resource_id_count": 0,
        "passed": True,
    }


def test_real_quality_gate_fails_closed_for_unavailable_metric() -> None:
    module = _module()
    report = _quality_report()
    report["metrics"]["clarification_accuracy"] = {  # type: ignore[index]
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }

    gate = module.assess_real_model_quality(report)

    assert gate["status"] == "failed"
    assert gate["checks"]["clarification_accuracy"]["passed"] is False


def test_real_quality_gate_fails_closed_for_malformed_resource_evidence() -> None:
    module = _module()
    report = _quality_report()
    report["results"] = [
        {
            "write_executed": False,
            "created_resource_ids": "not-a-list",
        }
    ]

    gate = module.assess_real_model_quality(report)

    assert gate["status"] == "failed"
    assert gate["safety"]["passed"] is False


def test_finalize_v2_report_persists_failed_gate_and_returns_exit_3(
    tmp_path: Path,
) -> None:
    module = _module()
    output = tmp_path / "real-report.json"
    report = _quality_report(tool_selection=0.94)

    exit_code = module.finalize_v2_report(report, output)

    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 3
    assert persisted["quality_gate"]["status"] == "failed"
    assert (
        persisted["quality_gate"]["checks"]["tool_selection_accuracy"]["passed"]
        is False
    )
