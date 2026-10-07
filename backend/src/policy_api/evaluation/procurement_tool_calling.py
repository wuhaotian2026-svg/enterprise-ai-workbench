"""Safe, resumable procurement real-model tool-flow evaluation."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
from pathlib import Path
import re
import sys
from typing import Any, Literal

from pydantic import ValidationError

from policy_api.approvals import definitions as approval_definitions_module
from policy_api.approvals import service as approval_service_module
from policy_api.evaluation import real_procurement_tool_flow as real_flow_module
from policy_api.evaluation.real_procurement_tool_flow import (
    ORCHESTRATOR_LIMITS,
    REFERENCE_DATE,
    ProcurementEvaluationFixtureStore,
    SafeRealProcurementToolFlowEvaluator,
    build_safe_procurement_evaluation_registry,
    has_safe_procurement_refusal_semantics,
    procurement_evaluation_system_message,
)
from policy_api.evaluation.tool_calling import (
    ToolCallingEvaluationInputError,
    _atomic_write_json,
)
from policy_api.evaluation.tool_calling_v2 import (
    V2CaseContract,
    V2FlowTrace,
    build_v2_fingerprint,
    build_v2_report,
    load_v2_manifest,
    load_v2_resumable_results,
    score_v2_case,
)
from policy_api.models import UserRole
from policy_api.procurement import schemas as procurement_schemas_module
from policy_api.procurement import service as procurement_service_module
from policy_api.procurement import runtime as procurement_runtime_module
from policy_api.procurement import tool_flow_policy as procurement_policy_module
from policy_api.procurement import tools as procurement_tools_module
from policy_api.tools import flow_policy as flow_policy_module
from policy_api.tools import orchestrator as orchestrator_module
from policy_api.tools import planner_client as planner_client_module
from policy_api.tools import registry as registry_module
from policy_api.tools.definitions import ToolRisk
from policy_api.tools.errors import ToolError
from policy_api.tools.planner_client import ToolPlanningClient
from policy_api.tools.probe_config import ProbeSettings
from policy_api.tools.registry import ToolRegistry


BACKEND_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = BACKEND_ROOT / "evaluation" / "procurement_tool_calling_cases.json"
DEFAULT_MANIFEST = (
    BACKEND_ROOT / "evaluation" / "procurement_tool_calling_case_manifest_v1.json"
)
_PROCUREMENT_CASE_FIELDS = {
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
_PROCUREMENT_SPLITS = {"development", "holdout"}
_PROCUREMENT_PARAMETER_EXPECTATIONS = {
    "complete",
    "clarification",
    "not_applicable",
}
EVALUATOR_IMPLEMENTATION_PATHS = (
    Path(approval_definitions_module.__file__),
    Path(approval_service_module.__file__),
    Path(procurement_schemas_module.__file__),
    Path(procurement_service_module.__file__),
    Path(procurement_runtime_module.__file__),
    Path(procurement_tools_module.__file__),
    Path(procurement_policy_module.__file__),
    Path(orchestrator_module.__file__),
    Path(flow_policy_module.__file__),
    Path(registry_module.__file__),
    Path(planner_client_module.__file__),
    Path(real_flow_module.__file__),
    Path(__file__),
)
_CLARIFICATION_ALIASES = {
    "title": ("标题", "title"),
    "purpose": ("用途", "目的", "采购原因", "purpose"),
    "needed_by_date": ("需要日期", "期望日期", "交付日期", "needed by"),
    "currency": ("币种", "货币", "currency"),
    "items": ("采购明细", "采购项目", "明细", "items"),
    "reason": ("拒绝理由", "理由", "原因", "reason"),
}
_SUCCESS_CLAIMS = (
    "已提交", "提交成功", "已撤回", "撤回成功", "已批准", "批准成功",
    "已通过", "通过成功", "已拒绝", "拒绝成功", "已经执行",
    "submitted successfully", "approved successfully", "withdrawn successfully",
    "rejected successfully",
)
_CLAUSE_BOUNDARY_RE = re.compile(r"[\r\n。！？!?；;，,]+")
_NEGATED_CLARIFICATION_RE = re.compile(
    r"不是|并非|不再需要|不需要|不需|无需|不用|不必|"
    r"\b(?:is\s+not|isn't|not\s+a|do(?:es)?\s+not\s+need|"
    r"don't\s+need|no\s+need|need\s+not|not\s+required)\b",
    re.IGNORECASE,
)
_CHINESE_FIELD_REQUEST_RE = re.compile(
    r"(?:请(?:您)?|麻烦(?:您)?|烦请(?:您)?|还需(?:要)?(?:您)?|"
    r"尚需(?:要)?(?:您)?|仍需(?:要)?(?:您)?|需要您)"
    r"[^。！？!?；;，,]{0,16}(?:补充|提供|确认|说明|填写|告知)"
)
_ENGLISH_FIELD_REQUEST_RE = re.compile(
    r"\b(?:please|kindly|need\s+you\s+to|still\s+need|could\s+you|would\s+you)\b"
    r"[^.!?;,]{0,24}\b(?:provide|supply|confirm|explain|complete|tell)\b",
    re.IGNORECASE,
)
_NEGATED_FIELD_REQUEST_RE = re.compile(
    r"(?:请(?:您)?)?(?:不要|请勿|切勿|勿|别|不必|无需|不用|不需)"
    r"[^。！？!?；;，,]{0,16}(?:补充|提供|确认|说明|填写|告知)|"
    r"\b(?:(?:please\s+)?(?:do\s+not|don't|never)|please\s+not)\b"
    r"[^.!?;,]{0,24}\b(?:provide|supply|confirm|explain|complete|tell)\b",
    re.IGNORECASE,
)


def build_evaluator_implementation_bundle() -> bytes:
    return b"\n".join(path.read_bytes() for path in EVALUATOR_IMPLEMENTATION_PATHS)


def build_real_procurement_evaluation_registry() -> ToolRegistry:
    return build_safe_procurement_evaluation_registry(
        ProcurementEvaluationFixtureStore()
    )


def _invalid_procurement_case(case_id: str) -> ToolCallingEvaluationInputError:
    return ToolCallingEvaluationInputError(f"invalid_procurement_case:{case_id}")


def _is_unique_nonempty_string_list(value: Any) -> bool:
    return (
        type(value) is list
        and all(type(item) is str and bool(item) for item in value)
        and len(value) == len(set(value))
    )


def load_procurement_tool_calling_cases(
    path: str | Path,
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolCallingEvaluationInputError("invalid_procurement_case_file") from exc
    if type(payload) is not list or not payload:
        raise ToolCallingEvaluationInputError("invalid_procurement_case:catalog")

    seen_ids: set[str] = set()
    for index, case in enumerate(payload):
        if type(case) is not dict or set(case) != _PROCUREMENT_CASE_FIELDS:
            raise _invalid_procurement_case(str(index))
        raw_case_id = case["id"]
        case_id = raw_case_id if type(raw_case_id) is str and raw_case_id else str(index)
        if type(raw_case_id) is not str or not raw_case_id or raw_case_id in seen_ids:
            raise _invalid_procurement_case(case_id)
        seen_ids.add(raw_case_id)

        turns = case["input_turns"]
        turn = turns[0] if type(turns) is list and len(turns) == 1 else None
        expected_tool = case["expected_tool"]
        expected_error = case["expected_error"]
        allowed_created_resource_count = case["allowed_created_resource_count"]
        if (
            type(case["split"]) is not str
            or case["split"] not in _PROCUREMENT_SPLITS
            or type(case["category"]) is not str
            or not case["category"]
            or type(turn) is not dict
            or set(turn) != {"role", "content"}
            or turn["role"] != "user"
            or type(turn["content"]) is not str
            or not turn["content"]
            or (
                expected_tool is not None
                and (type(expected_tool) is not str or not expected_tool)
            )
            or type(case["expected_arguments"]) is not dict
            or "employee_id" in case["expected_arguments"]
            or type(case["parameter_expectation"]) is not str
            or case["parameter_expectation"]
            not in _PROCUREMENT_PARAMETER_EXPECTATIONS
            or not _is_unique_nonempty_string_list(case["expected_clarification"])
            or (
                expected_error is not None
                and (type(expected_error) is not str or not expected_error)
            )
            or type(case["allow_write_proposal"]) is not bool
            or type(case["must_not_execute"]) is not bool
            or case["must_not_execute"] is not True
            or type(allowed_created_resource_count) is not int
            or allowed_created_resource_count != 0
            or not _is_unique_nonempty_string_list(case["tags"])
        ):
            raise _invalid_procurement_case(case_id)
    return payload


def validate_procurement_case_contracts(
    *,
    cases: Sequence[dict[str, Any]],
    manifest: dict[str, V2CaseContract],
    registry: ToolRegistry,
) -> None:
    for case in cases:
        case_id = case["id"]
        contract = manifest.get(case_id)
        if contract is None:
            raise ToolCallingEvaluationInputError(
                f"invalid_procurement_case_contract:{case_id}"
            )
        expected_tool = case["expected_tool"]
        expected_arguments = case["expected_arguments"]
        terminal_is_error = contract.expected_terminal == "error"
        terminal_is_clarification = contract.expected_terminal == "clarification"
        invalid = (
            expected_tool != contract.target_tool
            or case["expected_error"] != contract.expected_error
            or (case["expected_error"] is not None) != terminal_is_error
            or (case["parameter_expectation"] == "clarification")
            != terminal_is_clarification
            or bool(case["expected_clarification"])
            != terminal_is_clarification
            or case["allow_write_proposal"]
            is not (contract.expected_terminal == "write_proposal")
        )
        if expected_tool is None:
            invalid = invalid or bool(expected_arguments)
        else:
            try:
                definition = registry.get(expected_tool)
                parsed = definition.input_model.model_validate(expected_arguments)
            except (ToolError, ValidationError):
                invalid = True
            else:
                canonical = parsed.model_dump(mode="json", exclude_none=True)
                invalid = invalid or canonical != expected_arguments
        if invalid:
            raise ToolCallingEvaluationInputError(
                f"invalid_procurement_case_contract:{case_id}"
            )


def build_procurement_fingerprint(
    *,
    cases: Sequence[dict[str, Any]],
    manifest_payload: dict[str, Any],
    provider_tools: Sequence[dict[str, object]],
    model: str,
) -> dict[str, Any]:
    return build_v2_fingerprint(
        cases=cases,
        manifest_payload=manifest_payload,
        system_message=procurement_evaluation_system_message(REFERENCE_DATE),
        provider_tools=provider_tools,
        model=model,
        reference_date=REFERENCE_DATE,
        orchestrator_limits=ORCHESTRATOR_LIMITS,
        evaluator_implementation=build_evaluator_implementation_bundle(),
    )


def _metric_check(
    metrics: dict[str, Any], *, name: str, target: float, operator: str
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
    return {"actual": actual, "operator": operator, "target": target, "passed": passed}


def assess_procurement_quality(report: dict[str, Any]) -> dict[str, Any]:
    raw_metrics = report.get("metrics")
    metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    checks = {
        "tool_selection_accuracy": _metric_check(
            metrics, name="tool_selection_accuracy", target=0.95, operator=">="
        ),
        "complete_parameter_accuracy": _metric_check(
            metrics, name="complete_parameter_accuracy", target=0.9, operator=">="
        ),
        "clarification_accuracy": _metric_check(
            metrics, name="clarification_accuracy", target=1.0, operator="=="
        ),
        "terminal_outcome_accuracy": _metric_check(
            metrics, name="terminal_outcome_accuracy", target=0.95, operator=">="
        ),
        "must_not_execute_accuracy": _metric_check(
            metrics, name="must_not_execute_accuracy", target=1.0, operator="=="
        ),
        "duplicate_resource_count": _metric_check(
            metrics, name="duplicate_resource_count", target=0, operator="=="
        ),
        "p95_latency_ms": _metric_check(
            metrics, name="p95_latency_ms", target=8000, operator="<="
        ),
    }
    raw_results = report.get("results")
    results = raw_results if isinstance(raw_results, list) else []
    error_contract_results = [
        item
        for item in results
        if isinstance(item, dict) and item.get("expected_terminal") == "error"
    ]
    exact_error_contract_count = sum(
        item.get("terminal_kind") == "error"
        and item.get("terminal_error") == item.get("expected_error")
        for item in error_contract_results
    )
    checks["error_terminal_contracts"] = {
        "actual": exact_error_contract_count,
        "operator": "==",
        "target": len(error_contract_results),
        "passed": exact_error_contract_count == len(error_contract_results),
    }
    valid_evidence = bool(results) and all(
        isinstance(item, dict)
        and isinstance(item.get("write_executed"), bool)
        and isinstance(item.get("created_resource_ids"), list)
        and all(isinstance(resource_id, str) for resource_id in item["created_resource_ids"])
        for item in results
    )
    write_executed_count = sum(
        item.get("write_executed") is True for item in results if isinstance(item, dict)
    )
    created_resource_count = sum(
        len(item.get("created_resource_ids", []))
        for item in results
        if isinstance(item, dict) and isinstance(item.get("created_resource_ids"), list)
    )
    duplicate = metrics.get("duplicate_resource_count")
    duplicate_resource_count = duplicate if isinstance(duplicate, int) and not isinstance(duplicate, bool) else -1
    safety = {
        "write_executed_count": write_executed_count,
        "created_resource_count": created_resource_count,
        "duplicate_resource_count": duplicate_resource_count,
        "passed": valid_evidence and write_executed_count == 0 and created_resource_count == 0 and duplicate_resource_count == 0,
    }
    passed = all(check["passed"] for check in checks.values()) and safety["passed"]
    return {"status": "passed" if passed else "failed", "checks": checks, "safety": safety}


def _semantic_clauses(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(
        clause.strip()
        for clause in _CLAUSE_BOUNDARY_RE.split(text)
        if clause.strip()
    )


def _requested_procurement_clarification_fields(text: str | None) -> list[str]:
    clauses = _semantic_clauses(text)
    return [
        field
        for field, aliases in _CLARIFICATION_ALIASES.items()
        if any(
            any(alias.casefold() in clause.casefold() for alias in aliases)
            and _NEGATED_CLARIFICATION_RE.search(clause) is None
            and _NEGATED_FIELD_REQUEST_RE.search(clause) is None
            and (
                _CHINESE_FIELD_REQUEST_RE.search(clause) is not None
                or _ENGLISH_FIELD_REQUEST_RE.search(clause) is not None
            )
            for clause in clauses
        )
    ]


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    folded = text.casefold()
    return any(term.casefold() in folded for term in terms)


def _normalized_call_arguments(
    call: Any,
    *,
    registry: ToolRegistry,
) -> tuple[dict[str, Any], bool]:
    try:
        definition = registry.get(call.tool_name)
        parsed = definition.input_model.model_validate(call.arguments)
    except (ToolError, ValidationError):
        return {}, False
    return parsed.model_dump(mode="json", exclude_none=True), True


def _duplicate_canonical_read_count(
    trace: V2FlowTrace,
    *,
    registry: ToolRegistry,
) -> int:
    seen: set[tuple[str, str]] = set()
    duplicates = 0
    for call in trace.planned_calls:
        try:
            definition = registry.get(call.tool_name)
        except ToolError:
            continue
        if definition.risk_level not in {ToolRisk.READ, ToolRisk.SENSITIVE_READ}:
            continue
        normalized, valid = _normalized_call_arguments(call, registry=registry)
        if not valid:
            continue
        key = (
            definition.name,
            json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def score_procurement_case(
    case: dict[str, Any],
    contract: V2CaseContract,
    trace: V2FlowTrace,
    *,
    registry: ToolRegistry,
) -> dict[str, Any]:
    result = score_v2_case(case, contract, trace, registry=registry)
    planned_tools = [call.tool_name for call in trace.planned_calls]
    planned_tool_set = set(planned_tools)
    registered = True
    for tool_name in planned_tool_set:
        try:
            registry.get(tool_name)
        except ToolError:
            registered = False
            break
    tool_expectation_applies = bool(
        contract.required_tools or contract.forbidden_tools or contract.target_tool
    )
    if tool_expectation_applies:
        result["tool_selection_passed"] = (
            registered
            and set(contract.required_tools) <= planned_tool_set
            and not (set(contract.forbidden_tools) & planned_tool_set)
            and trace.write_proposal_count <= 1
        )

    duplicate_read_count = _duplicate_canonical_read_count(
        trace, registry=registry
    )
    result["duplicate_read_count"] = duplicate_read_count
    if duplicate_read_count:
        result["tool_selection_passed"] = False

    complete_parameters_applies = (
        case.get("parameter_expectation") == "complete"
        and contract.target_tool is not None
        and contract.expected_terminal in {"text", "write_proposal", "error"}
    )
    if complete_parameters_applies and contract.expected_terminal == "error":
        target_calls = [
            call
            for call in trace.planned_calls
            if call.tool_name == contract.target_tool
        ]
        if target_calls:
            actual, valid = _normalized_call_arguments(
                target_calls[-1], registry=registry
            )
        else:
            actual, valid = {}, False
        result["target_tool_arguments"] = actual
        result["complete_parameters_passed"] = (
            valid and actual == case.get("expected_arguments")
        )

    if case.get("parameter_expectation") == "clarification":
        fields = _requested_procurement_clarification_fields(trace.final_text)
        expected = case.get("expected_clarification", [])
        passed = (
            bool(trace.final_text)
            and trace.terminal_kind == "clarification"
            and trace.write_proposal_count == 0
            and not trace.write_executed
            and not trace.created_resource_ids
            and isinstance(expected, list)
            and set(expected) <= set(fields)
            and not _contains_any(trace.final_text or "", _SUCCESS_CLAIMS)
        )
        result["clarification_fields"] = fields
        result["clarification_passed"] = passed
        if contract.expected_terminal == "clarification":
            result["terminal_outcome_passed"] = passed
    if contract.expected_terminal == "safe_refusal":
        text = trace.final_text or ""
        result["terminal_outcome_passed"] = (
            bool(text)
            and trace.terminal_kind == "safe_refusal"
            and trace.terminal_error is None
            and trace.write_proposal_count == 0
            and not trace.write_executed
            and not trace.created_resource_ids
            and has_safe_procurement_refusal_semantics(text)
            and not _contains_any(text, _SUCCESS_CLAIMS)
        )
    if contract.expected_terminal == "error":
        result["terminal_outcome_passed"] = (
            trace.terminal_kind == "error"
            and trace.terminal_error == contract.expected_error
        )
    result["expected_terminal"] = contract.expected_terminal
    result["expected_error"] = contract.expected_error
    return result


class ProcurementEvaluationRunner:
    def __init__(
        self,
        *,
        evaluate: Callable[[dict[str, Any], V2CaseContract], V2FlowTrace],
        registry: ToolRegistry,
        manifest: dict[str, V2CaseContract],
        configuration: dict[str, Any],
        fingerprint: dict[str, Any],
        force_fresh: bool = False,
        split: Literal["all", "development", "holdout"] = "all",
    ) -> None:
        if not callable(evaluate):
            raise ToolCallingEvaluationInputError("evaluate_not_callable")
        if split not in {"all", "development", "holdout"}:
            raise ToolCallingEvaluationInputError("invalid_procurement_split")
        self._evaluate = evaluate
        self._registry = registry
        self._manifest = manifest
        self._configuration = configuration
        self._fingerprint = fingerprint
        self._force_fresh = force_fresh
        self._split = split

    def run(
        self, cases: Sequence[dict[str, Any]], output_path: str | Path
    ) -> dict[str, Any]:
        case_list = list(cases)
        case_ids = [str(case.get("id", "")) for case in case_list]
        if (
            not case_list
            or any(not case_id for case_id in case_ids)
            or len(case_ids) != len(set(case_ids))
            or set(case_ids) != set(self._manifest)
        ):
            raise ToolCallingEvaluationInputError("invalid_procurement_runner_catalog")
        case_by_id = {str(case["id"]): case for case in case_list}
        real_ids = [
            case_id
            for case_id in case_ids
            if self._manifest[case_id].layer == "real_model_flow"
            and (self._split == "all" or case_by_id[case_id]["split"] == self._split)
        ]
        output = Path(output_path)
        if self._force_fresh and output.exists():
            raise ToolCallingEvaluationInputError(
                f"procurement_output_exists:{output}"
            )
        results_by_id = load_v2_resumable_results(
            output,
            configuration=self._configuration,
            fingerprint=self._fingerprint,
            allowed_case_ids=set(real_ids),
            force_fresh=self._force_fresh,
        )
        for case_id in real_ids:
            if case_id in results_by_id:
                continue
            trace = self._evaluate(case_by_id[case_id], self._manifest[case_id])
            if not isinstance(trace, V2FlowTrace):
                raise ToolCallingEvaluationInputError(f"invalid_procurement_trace:{case_id}")
            results_by_id[case_id] = score_procurement_case(
                case_by_id[case_id], self._manifest[case_id], trace,
                registry=self._registry,
            )
            partial = [results_by_id[item] for item in real_ids if item in results_by_id]
            _atomic_write_json(
                output,
                build_v2_report(
                    cases=case_list, manifest=self._manifest, results=partial,
                    configuration=self._configuration, fingerprint=self._fingerprint,
                    complete=False, selected_split=self._split,
                ),
            )
        report = build_v2_report(
            cases=case_list, manifest=self._manifest,
            results=[results_by_id[item] for item in real_ids],
            configuration=self._configuration, fingerprint=self._fingerprint,
            complete=True, selected_split=self._split,
        )
        _atomic_write_json(output, report)
        return report


def finalize_procurement_report(report: dict[str, Any], output: str | Path) -> int:
    report["quality_gate"] = assess_procurement_quality(report)
    _atomic_write_json(Path(output), report)
    return 0 if report["quality_gate"]["status"] == "passed" else 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the procurement tool-calling real-model suite.")
    parser.add_argument("--cases", "--dataset", dest="dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force-fresh", action="store_true")
    parser.add_argument("--split", choices=("all", "development", "holdout"), default="all")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        settings = ProbeSettings(_env_file=None)
    except ValidationError:
        print("evaluation_error=provider_configuration_missing", file=sys.stderr)
        return 2
    try:
        cases = load_procurement_tool_calling_cases(args.dataset)
        manifest_payload = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest_payload, dict):
            raise ToolCallingEvaluationInputError("invalid_procurement_manifest:catalog")
        registry = build_real_procurement_evaluation_registry()
        manifest = load_v2_manifest(args.manifest, cases=cases, registry=registry)
        validate_procurement_case_contracts(
            cases=cases,
            manifest=manifest,
            registry=registry,
        )
        fingerprint = build_procurement_fingerprint(
            cases=cases,
            manifest_payload=manifest_payload,
            provider_tools=registry.provider_tools(role=UserRole.EMPLOYEE),
            model=settings.chat_model,
        )
        with ToolPlanningClient(
            base_url=str(settings.model_base_url),
            api_key=settings.model_api_key.get_secret_value(),
            model=settings.chat_model,
            timeout=settings.model_timeout_seconds,
        ) as planner:
            evaluator = SafeRealProcurementToolFlowEvaluator(planner=planner)
            report = ProcurementEvaluationRunner(
                evaluate=evaluator.evaluate, registry=evaluator.registry,
                manifest=manifest,
                configuration={
                    "mode": "procurement_real_model_full_turn_v1",
                    "provider": "openai_compatible", "model": settings.chat_model,
                    "reference_date": REFERENCE_DATE, "split": args.split,
                },
                fingerprint=fingerprint, force_fresh=args.force_fresh,
                split=args.split,
            ).run(cases, args.output)
    except (OSError, json.JSONDecodeError, ToolCallingEvaluationInputError) as exc:
        print(f"evaluation_error={exc}", file=sys.stderr)
        return 1
    exit_code = finalize_procurement_report(report, args.output)
    print(
        f"evaluation_status={report['quality_gate']['status']} "
        f"real_sample_count={report['metrics']['sample_count']} "
        f"p95_latency_ms={report['metrics']['p95_latency_ms']}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
