from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy.orm import Session

from policy_api.evaluation import real_tool_flow as real_tool_flow_module
from policy_api.evaluation import tool_calling_v2 as tool_calling_v2_module
from policy_api.hr import tool_flow_policy as hr_tool_flow_policy_module
from policy_api.hr import tools as hr_tools_module
from policy_api.tools import flow_policy as flow_policy_module
from policy_api.tools import orchestrator as orchestrator_module
from policy_api.tools import planner_client as planner_client_module
from policy_api.tools import registry as registry_module
from policy_api.evaluation.real_tool_flow import (
    ORCHESTRATOR_LIMITS,
    SafeRealToolFlowEvaluator,
    evaluation_system_message,
)
from policy_api.evaluation.tool_calling import (
    ToolCallingEvaluationInputError,
    _atomic_write_json,
    load_tool_calling_cases,
)
from policy_api.evaluation.tool_calling_v2 import (
    V2EvaluationRunner,
    build_v2_fingerprint,
    load_v2_manifest,
)
from policy_api.hr.tools import build_hr_tool_definitions
from policy_api.knowledge.tools import (
    PolicySearchOutcome,
    build_knowledge_tool_definition,
)
from policy_api.models import UserRole
from policy_api.tools.definitions import ToolRisk
from policy_api.tools.errors import ToolError
from policy_api.tools.orchestrator import Planner, SYSTEM_MESSAGE
from policy_api.tools.planner_client import PlannerError, ToolPlanningClient
from policy_api.tools.probe_config import ProbeSettings
from policy_api.tools.registry import ToolRegistry


REFERENCE_DATE = "2026-08-16"
BACKEND_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = BACKEND_ROOT / "evaluation" / "hr_tool_calling_cases.json"
DEFAULT_MANIFEST = (
    BACKEND_ROOT / "evaluation" / "hr_tool_calling_case_manifest_v2.json"
)
_SECRET_KEY_PARTS = ("api_key", "authorization", "password", "secret", "token")
EVALUATOR_IMPLEMENTATION_PATHS = (
    Path(orchestrator_module.__file__),
    Path(flow_policy_module.__file__),
    Path(registry_module.__file__),
    Path(planner_client_module.__file__),
    Path(hr_tool_flow_policy_module.__file__),
    Path(hr_tools_module.__file__),
    Path(real_tool_flow_module.__file__),
    Path(tool_calling_v2_module.__file__),
)


def build_evaluator_implementation_bundle() -> bytes:
    return b"\n".join(
        path.read_bytes() for path in EVALUATOR_IMPLEMENTATION_PATHS
    )


def _redact_model_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_model_payload(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SECRET_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_redact_model_payload(item) for item in value]
    return value


def _metric_check(
    metrics: dict[str, Any],
    *,
    name: str,
    target: float,
    operator: str,
) -> dict[str, Any]:
    metric = metrics.get(name)
    actual = metric.get("value") if isinstance(metric, dict) else metric
    if not isinstance(actual, int | float) or isinstance(actual, bool):
        passed = False
    elif operator == ">=":
        passed = actual >= target
    elif operator == "<=":
        passed = actual <= target
    elif operator == "==":
        passed = actual == target
    else:
        raise ValueError("unsupported_quality_operator")
    return {
        "actual": actual,
        "operator": operator,
        "target": target,
        "passed": passed,
    }


def assess_real_model_quality(report: dict[str, Any]) -> dict[str, Any]:
    raw_metrics = report.get("metrics")
    metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    checks = {
        "tool_selection_accuracy": _metric_check(
            metrics,
            name="tool_selection_accuracy",
            target=0.95,
            operator=">=",
        ),
        "complete_parameter_accuracy": _metric_check(
            metrics,
            name="complete_parameter_accuracy",
            target=0.9,
            operator=">=",
        ),
        "clarification_accuracy": _metric_check(
            metrics,
            name="clarification_accuracy",
            target=0.95,
            operator=">=",
        ),
        "terminal_outcome_accuracy": _metric_check(
            metrics,
            name="terminal_outcome_accuracy",
            target=0.95,
            operator=">=",
        ),
        "must_not_execute_accuracy": _metric_check(
            metrics,
            name="must_not_execute_accuracy",
            target=1.0,
            operator="==",
        ),
        "duplicate_resource_count": _metric_check(
            metrics,
            name="duplicate_resource_count",
            target=0,
            operator="==",
        ),
        "p95_latency_ms": _metric_check(
            metrics,
            name="p95_latency_ms",
            target=8000,
            operator="<=",
        ),
    }
    raw_results = report.get("results")
    results = raw_results if isinstance(raw_results, list) else []
    valid_result_evidence = bool(results) and all(
        isinstance(item, dict)
        and isinstance(item.get("write_executed"), bool)
        and isinstance(item.get("created_resource_ids"), list)
        and all(
            isinstance(resource_id, str)
            for resource_id in item.get("created_resource_ids", [])
        )
        for item in results
    )
    write_executed_count = sum(
        bool(item.get("write_executed"))
        for item in results
        if isinstance(item, dict)
    )
    created_resource_id_count = sum(
        len(item.get("created_resource_ids", []))
        for item in results
        if isinstance(item, dict)
        and isinstance(item.get("created_resource_ids", []), list)
    )
    safety = {
        "write_executed_count": write_executed_count,
        "created_resource_id_count": created_resource_id_count,
        "passed": (
            valid_result_evidence
            and write_executed_count == 0
            and created_resource_id_count == 0
        ),
    }
    passed = all(check["passed"] for check in checks.values()) and safety["passed"]
    return {
        "status": "passed" if passed else "failed",
        "checks": checks,
        "safety": safety,
    }


def finalize_v2_report(
    report: dict[str, Any],
    output: str | Path,
) -> int:
    report["quality_gate"] = assess_real_model_quality(report)
    _atomic_write_json(Path(output), report)
    return 0 if report["quality_gate"]["status"] == "passed" else 3


def finalize_real_model_report(
    report: dict[str, Any],
    output: str | Path,
) -> int:
    """Historical compatibility wrapper for V1 callers and tests."""
    return finalize_v2_report(report, output)


def _unreachable_policy_search(_query: str) -> PolicySearchOutcome:
    raise RuntimeError("evaluation_tool_execution_forbidden")


def build_real_evaluation_registry() -> ToolRegistry:
    definitions = (
        build_knowledge_tool_definition(_unreachable_policy_search),
        *build_hr_tool_definitions(cast(Session, None)),
    )
    return ToolRegistry(definitions)


class RealModelToolCallingEvaluator:
    """Observe one real planner turn without executing any tool or business write."""

    def __init__(
        self,
        *,
        planner: Planner,
        registry: ToolRegistry,
        reference_date: str = REFERENCE_DATE,
    ) -> None:
        self._planner = planner
        self._registry = registry
        self._system_message = (
            f"{SYSTEM_MESSAGE} The evaluation reference date is {reference_date}. "
            "Use that date when resolving relative dates."
        )
        self._provider_tools = list(
            registry.provider_tools(role=UserRole.EMPLOYEE)
        )

    def __call__(self, case: dict[str, Any]) -> dict[str, Any]:
        messages = self._messages(case)
        started = perf_counter()
        try:
            turn = self._planner.complete(messages, tools=self._provider_tools)
        except PlannerError as exc:
            return self._observation(
                latency_ms=self._latency_ms(started),
                error_code=exc.code,
            )

        if len(turn.tool_calls) > 1:
            return self._observation(
                latency_ms=self._latency_ms(started),
                error_code="multiple_tool_calls",
            )
        if not turn.tool_calls:
            return self._observation(latency_ms=self._latency_ms(started))

        tool_call = turn.tool_calls[0]
        try:
            definition = self._registry.resolve_provider_name(tool_call.name)
        except ToolError as exc:
            return self._observation(
                latency_ms=self._latency_ms(started),
                error_code=exc.code,
            )

        arguments = dict(tool_call.arguments)
        schema = definition.input_model.model_json_schema()
        required = {
            str(field) for field in schema.get("required", [])
            if isinstance(field, str)
        }
        missing = sorted(required - set(arguments))
        validation_error = False
        try:
            parsed = definition.input_model.model_validate(arguments)
            normalized = parsed.model_dump(mode="json", exclude_none=True)
        except ValidationError:
            validation_error = True
            normalized = self._json_mapping(arguments)

        return self._observation(
            latency_ms=self._latency_ms(started),
            selected_tool=definition.name,
            normalized_arguments=normalized,
            clarification_fields=missing,
            error_code=(
                "invalid_tool_arguments"
                if validation_error and not missing
                else None
            ),
            write_proposed=(
                definition.risk_level == ToolRisk.WRITE
                and not validation_error
            ),
        )

    def _messages(self, case: dict[str, Any]) -> list[dict[str, object]]:
        input_turns = case.get("input_turns")
        if not isinstance(input_turns, list) or not input_turns:
            raise ToolCallingEvaluationInputError("invalid_input_turns")
        messages: list[dict[str, object]] = [
            {"role": "system", "content": self._system_message}
        ]
        for turn in input_turns:
            if not isinstance(turn, dict):
                raise ToolCallingEvaluationInputError("invalid_input_turn")
            role = turn.get("role")
            content = turn.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise ToolCallingEvaluationInputError("invalid_input_turn")
            messages.append({"role": role, "content": content})
        return messages

    @staticmethod
    def _json_mapping(arguments: dict[str, object]) -> dict[str, object]:
        try:
            value = json.loads(json.dumps(arguments, ensure_ascii=False))
        except (TypeError, ValueError):
            return {}
        redacted = _redact_model_payload(value)
        return redacted if isinstance(redacted, dict) else {}

    @staticmethod
    def _latency_ms(started: float) -> int:
        return max(0, round((perf_counter() - started) * 1000))

    @staticmethod
    def _observation(
        *,
        latency_ms: int,
        selected_tool: str | None = None,
        normalized_arguments: dict[str, object] | None = None,
        clarification_fields: Sequence[str] = (),
        error_code: str | None = None,
        write_proposed: bool = False,
    ) -> dict[str, Any]:
        return {
            "selected_tool": selected_tool,
            "normalized_arguments": normalized_arguments or {},
            "clarification_fields": list(clarification_fields),
            "error_code": error_code,
            "write_proposed": write_proposed,
            "write_executed": False,
            "created_resource_ids": [],
            "latency_ms": latency_ms,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed HR Tool Calling set against an explicit real provider."
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force-fresh", action="store_true")
    parser.add_argument(
        "--split",
        choices=("all", "development", "holdout"),
        default="all",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        settings = ProbeSettings(_env_file=None)
    except ValidationError:
        print("evaluation_error=provider_configuration_missing", file=sys.stderr)
        return 2

    try:
        cases = load_tool_calling_cases(args.dataset)
        manifest_payload = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest_payload, dict):
            raise ToolCallingEvaluationInputError("invalid_v2_manifest:catalog")
        with ToolPlanningClient(
            base_url=str(settings.model_base_url),
            api_key=settings.model_api_key.get_secret_value(),
            model=settings.chat_model,
            timeout=settings.model_timeout_seconds,
        ) as planner:
            evaluator = SafeRealToolFlowEvaluator(
                planner=planner,
                reference_date=REFERENCE_DATE,
            )
            manifest = load_v2_manifest(
                args.manifest,
                cases=cases,
                registry=evaluator.registry,
            )
            implementation = build_evaluator_implementation_bundle()
            fingerprint = build_v2_fingerprint(
                cases=cases,
                manifest_payload=manifest_payload,
                system_message=evaluation_system_message(REFERENCE_DATE),
                provider_tools=evaluator.registry.provider_tools(
                    role=UserRole.EMPLOYEE
                ),
                model=settings.chat_model,
                reference_date=REFERENCE_DATE,
                orchestrator_limits=ORCHESTRATOR_LIMITS,
                evaluator_implementation=implementation,
            )
            report = V2EvaluationRunner(
                evaluate=evaluator.evaluate,
                registry=evaluator.registry,
                manifest=manifest,
                configuration={
                    "mode": "real_model_full_turn_v2",
                    "provider": "openai_compatible",
                    "model": settings.chat_model,
                    "reference_date": REFERENCE_DATE,
                    "split": args.split,
                },
                fingerprint=fingerprint,
                force_fresh=args.force_fresh,
                split=args.split,
            ).run(cases, args.output)
    except (OSError, json.JSONDecodeError, ToolCallingEvaluationInputError) as exc:
        print(f"evaluation_error={exc}", file=sys.stderr)
        return 1

    exit_code = finalize_v2_report(report, args.output)
    metrics = report["metrics"]
    print(
        f"evaluation_status={report['quality_gate']['status']} "
        f"real_sample_count={metrics['sample_count']} "
        f"excluded_count={len(report['excluded_cases'])} "
        f"p95_latency_ms={metrics['p95_latency_ms']}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
