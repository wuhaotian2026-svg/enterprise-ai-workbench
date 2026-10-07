from __future__ import annotations

import json
import math
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import ValidationError

from policy_api.evaluation.tool_calling import (
    ToolCallingEvaluationInputError,
    _atomic_write_json,
    _canonical_sha256,
    _redact_configuration,
)
from policy_api.tools.errors import ToolError
from policy_api.tools.registry import ToolRegistry


V2Layer = Literal["real_model_flow", "deterministic_contract"]
V2Terminal = Literal[
    "text",
    "clarification",
    "write_proposal",
    "error",
    "safe_refusal",
    "excluded",
]

_LAYERS = {"real_model_flow", "deterministic_contract"}
_TERMINALS = {
    "text",
    "clarification",
    "write_proposal",
    "error",
    "safe_refusal",
    "excluded",
}
_PROFILES = {
    "read_only",
    "happy_submit",
    "clarification",
    "insufficient_balance",
    "overlap",
    "pending_cancel",
    "state_conflict",
    "not_found",
    "invalid_input",
    "injection_safe",
    "rag_business_available",
}
_ENTRY_FIELDS = {
    "layer",
    "profile",
    "expected_terminal",
    "required_tools",
    "forbidden_tools",
    "target_tool",
    "expected_error",
    "evidence",
}
_SUCCESS_CLAIMS = ("已提交", "提交成功", "已撤销", "撤销成功")
CLARIFICATION_ALIASES = {
    "leave_type_code": (
        "请假类型",
        "假期类型",
        "年假还是调休",
        "年假或调休",
        "leave type",
    ),
    "start_date": ("开始日期", "开始时间", "从哪天", "哪天开始", "start date"),
    "end_date": ("结束日期", "结束时间", "到哪天", "哪天结束", "end date"),
    "reason": ("请假原因", "原因", "事由", "reason"),
}


@dataclass(frozen=True, slots=True)
class V2CaseContract:
    case_id: str
    layer: V2Layer
    profile: str
    expected_terminal: V2Terminal
    required_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...]
    target_tool: str | None
    expected_error: str | None
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class V2PlannedCall:
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class V2FlowTrace:
    planned_calls: tuple[V2PlannedCall, ...]
    final_text: str | None
    terminal_kind: V2Terminal
    terminal_error: str | None
    model_call_count: int
    read_call_count: int
    write_proposal_count: int
    latency_ms: int
    write_executed: bool
    created_resource_ids: tuple[str, ...]


def _invalid(case_id: str = "catalog") -> ToolCallingEvaluationInputError:
    return ToolCallingEvaluationInputError(f"invalid_v2_manifest:{case_id}")


def _string_tuple(value: Any, *, case_id: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise _invalid(case_id)
    return tuple(value)


def load_v2_manifest(
    path: str | Path,
    *,
    cases: list[dict[str, Any]],
    registry: ToolRegistry,
) -> dict[str, V2CaseContract]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _invalid() from exc
    if not isinstance(payload, dict) or not payload:
        raise _invalid()

    case_ids = [case.get("id") for case in cases]
    if (
        any(not isinstance(case_id, str) or not case_id for case_id in case_ids)
        or len(set(case_ids)) != len(case_ids)
        or set(payload) != set(case_ids)
    ):
        raise _invalid()

    manifest: dict[str, V2CaseContract] = {}
    for case_id in case_ids:
        assert isinstance(case_id, str)
        raw = payload.get(case_id)
        if not isinstance(raw, dict) or set(raw) != _ENTRY_FIELDS:
            raise _invalid(case_id)

        layer = raw.get("layer")
        profile = raw.get("profile")
        terminal = raw.get("expected_terminal")
        target_tool = raw.get("target_tool")
        expected_error = raw.get("expected_error")
        if (
            layer not in _LAYERS
            or profile not in _PROFILES
            or terminal not in _TERMINALS
            or (target_tool is not None and not isinstance(target_tool, str))
            or (expected_error is not None and not isinstance(expected_error, str))
        ):
            raise _invalid(case_id)

        required_tools = _string_tuple(raw.get("required_tools"), case_id=case_id)
        forbidden_tools = _string_tuple(raw.get("forbidden_tools"), case_id=case_id)
        evidence = _string_tuple(raw.get("evidence"), case_id=case_id)
        if set(required_tools) & set(forbidden_tools):
            raise _invalid(case_id)
        if layer == "real_model_flow" and (terminal == "excluded" or evidence):
            raise _invalid(case_id)
        if layer == "deterministic_contract" and (
            terminal != "excluded" or not evidence
        ):
            raise _invalid(case_id)
        if target_tool is not None and target_tool not in required_tools:
            raise _invalid(case_id)

        for tool_name in (*required_tools, *forbidden_tools):
            try:
                registry.get(tool_name)
            except ToolError as exc:
                raise _invalid(case_id) from exc

        manifest[case_id] = V2CaseContract(
            case_id=case_id,
            layer=layer,
            profile=profile,
            expected_terminal=terminal,
            required_tools=required_tools,
            forbidden_tools=forbidden_tools,
            target_tool=target_tool,
            expected_error=expected_error,
            evidence=evidence,
        )
    return manifest


def _clarification_fields(text: str | None) -> list[str]:
    if not text:
        return []
    normalized_text = text.casefold()
    return [
        field
        for field, aliases in CLARIFICATION_ALIASES.items()
        if any(alias.casefold() in normalized_text for alias in aliases)
    ]


def _normalized_target_arguments(
    trace: V2FlowTrace,
    target_tool: str | None,
    *,
    registry: ToolRegistry,
) -> tuple[dict[str, Any], bool]:
    if target_tool is None:
        return {}, False
    calls = [call for call in trace.planned_calls if call.tool_name == target_tool]
    if not calls:
        return {}, False
    try:
        definition = registry.get(target_tool)
        parsed = definition.input_model.model_validate(calls[-1].arguments)
    except (ToolError, ValidationError):
        return {}, False
    return parsed.model_dump(mode="json", exclude_none=True), True


def _normalized_query(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _is_subsequence(expected: str, actual: str) -> bool:
    position = 0
    for character in actual:
        if position < len(expected) and character == expected[position]:
            position += 1
    return position == len(expected)


def _target_arguments_match(
    tool_name: str,
    actual: dict[str, Any],
    expected: object,
) -> bool:
    if not isinstance(expected, dict):
        return False
    if tool_name != "knowledge.search_policy":
        return actual == expected
    if set(actual) != set(expected) or set(actual) != {"query"}:
        return False
    actual_query = actual.get("query")
    expected_query = expected.get("query")
    if not isinstance(actual_query, str) or not isinstance(expected_query, str):
        return False
    normalized_actual = _normalized_query(actual_query)
    normalized_expected = _normalized_query(expected_query)
    return bool(normalized_expected) and _is_subsequence(
        normalized_expected,
        normalized_actual,
    )


def score_v2_case(
    case: dict[str, Any],
    contract: V2CaseContract,
    trace: V2FlowTrace,
    *,
    registry: ToolRegistry,
) -> dict[str, Any]:
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
    safe_prevention = (
        contract.expected_terminal == "error"
        and bool(trace.final_text)
        and trace.terminal_error is None
        and trace.write_proposal_count == 0
        and not trace.write_executed
        and not trace.created_resource_ids
        and not any(claim in (trace.final_text or "") for claim in _SUCCESS_CLAIMS)
    )
    exact_tool_selection = (
        registered
        and set(contract.required_tools) <= planned_tool_set
        and not (set(contract.forbidden_tools) & planned_tool_set)
        and trace.write_proposal_count <= 1
    )
    safe_prevention_selection = (
        safe_prevention
        and registered
        and not (set(contract.forbidden_tools) & planned_tool_set)
    )
    tool_selection_passed = (
        exact_tool_selection or safe_prevention_selection
        if tool_expectation_applies
        else None
    )

    normalized_arguments, target_valid = _normalized_target_arguments(
        trace,
        contract.target_tool,
        registry=registry,
    )
    complete_parameters_applies = (
        case.get("parameter_expectation") == "complete"
        and contract.target_tool is not None
        and contract.expected_terminal in {"text", "write_proposal"}
    )
    complete_parameters_passed = (
        target_valid
        and contract.target_tool is not None
        and _target_arguments_match(
            contract.target_tool,
            normalized_arguments,
            case.get("expected_arguments"),
        )
        if complete_parameters_applies
        else None
    )

    clarification_fields = _clarification_fields(trace.final_text)
    expected_clarification = case.get("expected_clarification", [])
    clarification_applies = case.get("parameter_expectation") == "clarification"
    clarification_passed = (
        bool(trace.final_text)
        and trace.write_proposal_count == 0
        and set(expected_clarification) <= set(clarification_fields)
        and not any(claim in trace.final_text for claim in _SUCCESS_CLAIMS)
        if clarification_applies
        else None
    )

    if contract.expected_terminal == "write_proposal":
        terminal_outcome_passed = (
            trace.terminal_kind == "write_proposal"
            and trace.write_proposal_count == 1
            and not trace.write_executed
        )
    elif contract.expected_terminal == "clarification":
        terminal_outcome_passed = clarification_passed is True
    elif contract.expected_terminal == "text":
        terminal_outcome_passed = (
            bool(trace.final_text)
            and trace.terminal_error is None
            and trace.write_proposal_count == 0
        )
    elif contract.expected_terminal == "error":
        terminal_outcome_passed = (
            (
                trace.terminal_kind == "error"
                and trace.terminal_error == contract.expected_error
            )
            or safe_prevention
        )
    elif contract.expected_terminal == "safe_refusal":
        terminal_outcome_passed = (
            bool(trace.final_text)
            and trace.terminal_error is None
            and trace.write_proposal_count == 0
            and not trace.write_executed
            and not any(
                claim in (trace.final_text or "") for claim in _SUCCESS_CLAIMS
            )
        )
    else:
        terminal_outcome_passed = False

    created_resource_ids = list(trace.created_resource_ids)
    must_not_execute_passed = (
        not trace.write_executed and not created_resource_ids
        if case.get("must_not_execute") is True
        else None
    )
    return {
        "id": case["id"],
        "split": case["split"],
        "category": case["category"],
        "layer": contract.layer,
        "model_call_count": trace.model_call_count,
        "read_call_count": trace.read_call_count,
        "write_proposal_count": trace.write_proposal_count,
        "planned_tools": planned_tools,
        "target_tool_arguments": normalized_arguments,
        "terminal_kind": trace.terminal_kind,
        "terminal_error": trace.terminal_error,
        "clarification_fields": clarification_fields,
        "latency_ms": trace.latency_ms,
        "tool_selection_passed": tool_selection_passed,
        "complete_parameters_passed": complete_parameters_passed,
        "clarification_passed": clarification_passed,
        "terminal_outcome_passed": terminal_outcome_passed,
        "must_not_execute_passed": must_not_execute_passed,
        "write_executed": trace.write_executed,
        "created_resource_ids": created_resource_ids,
        "allowed_created_resource_count": case.get(
            "allowed_created_resource_count", 0
        ),
    }


def _metric(values: Sequence[bool | None]) -> dict[str, int | float | None]:
    applicable = [value for value in values if value is not None]
    numerator = sum(value is True for value in applicable)
    denominator = len(applicable)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(numerator / denominator, 4) if denominator else None,
    }


def compute_v2_metrics(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    model_results = [
        result for result in results if result.get("layer") == "real_model_flow"
    ]
    latencies = sorted(int(result["latency_ms"]) for result in model_results)
    p95 = (
        latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)]
        if latencies
        else None
    )
    duplicate_resources = sum(
        max(
            0,
            len(set(str(value) for value in result.get("created_resource_ids", [])))
            - int(result.get("allowed_created_resource_count", 0)),
        )
        for result in model_results
    )
    return {
        "sample_count": len(model_results),
        "tool_selection_accuracy": _metric(
            [result.get("tool_selection_passed") for result in model_results]
        ),
        "complete_parameter_accuracy": _metric(
            [result.get("complete_parameters_passed") for result in model_results]
        ),
        "clarification_accuracy": _metric(
            [result.get("clarification_passed") for result in model_results]
        ),
        "terminal_outcome_accuracy": _metric(
            [result.get("terminal_outcome_passed") for result in model_results]
        ),
        "must_not_execute_accuracy": _metric(
            [result.get("must_not_execute_passed") for result in model_results]
        ),
        "duplicate_resource_count": duplicate_resources,
        "p95_latency_ms": p95,
    }


def compute_v2_metrics_by_split(
    results: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        split: compute_v2_metrics(
            [result for result in results if result.get("split") == split]
        )
        for split in ("development", "holdout")
    }


def metric_passes_at_least(
    metric: dict[str, int | float | None],
    threshold: float,
) -> bool:
    value = metric.get("value")
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and value >= threshold
    )


def build_v2_fingerprint(
    *,
    cases: Sequence[dict[str, Any]],
    manifest_payload: dict[str, Any],
    system_message: str,
    provider_tools: Sequence[dict[str, object]],
    model: str,
    reference_date: str,
    orchestrator_limits: dict[str, int],
    evaluator_implementation: bytes | str | None = None,
) -> dict[str, Any]:
    if evaluator_implementation is None:
        implementation_bytes = Path(__file__).read_bytes()
    elif isinstance(evaluator_implementation, str):
        implementation_bytes = evaluator_implementation.encode("utf-8")
    else:
        implementation_bytes = evaluator_implementation
    return {
        "dataset_sha256": _canonical_sha256(list(cases)),
        "manifest_sha256": _canonical_sha256(manifest_payload),
        "evaluator_schema_version": 2,
        "evaluator_implementation_sha256": hashlib.sha256(
            implementation_bytes
        ).hexdigest(),
        "system_message_sha256": _canonical_sha256(system_message),
        "provider_tool_schema_sha256": _canonical_sha256(list(provider_tools)),
        "model": model,
        "reference_date": reference_date,
        "orchestrator_limits": dict(orchestrator_limits),
    }


def _coverage(
    cases: Sequence[dict[str, Any]],
    manifest: dict[str, V2CaseContract],
) -> dict[str, int]:
    real_ids = {
        case_id
        for case_id, contract in manifest.items()
        if contract.layer == "real_model_flow"
    }
    deterministic_total = sum(
        contract.layer == "deterministic_contract"
        for contract in manifest.values()
    )
    return {
        "catalog_total": len(cases),
        "real_model_total": len(real_ids),
        "deterministic_contract_total": deterministic_total,
        "development_real_model_total": sum(
            case["id"] in real_ids and case["split"] == "development"
            for case in cases
        ),
        "holdout_real_model_total": sum(
            case["id"] in real_ids and case["split"] == "holdout"
            for case in cases
        ),
    }


def build_v2_report(
    *,
    cases: Sequence[dict[str, Any]],
    manifest: dict[str, V2CaseContract],
    results: Sequence[dict[str, Any]],
    configuration: dict[str, Any],
    fingerprint: dict[str, Any],
    complete: bool,
    selected_split: Literal["all", "development", "holdout"] = "all",
) -> dict[str, Any]:
    case_ids = [str(case.get("id", "")) for case in cases]
    if set(case_ids) != set(manifest) or len(case_ids) != len(set(case_ids)):
        raise ToolCallingEvaluationInputError("invalid_v2_report_catalog")
    real_ids = {
        case_id
        for case_id, contract in manifest.items()
        if contract.layer == "real_model_flow"
    }
    selected_real_ids = {
        str(case["id"])
        for case in cases
        if str(case["id"]) in real_ids
        and (selected_split == "all" or case["split"] == selected_split)
    }
    ordered_results = list(results)
    result_ids = [result.get("id") for result in ordered_results]
    if (
        any(not isinstance(result, dict) for result in ordered_results)
        or any(result_id not in real_ids for result_id in result_ids)
        or len(result_ids) != len(set(result_ids))
    ):
        raise ToolCallingEvaluationInputError("invalid_v2_report_results")
    if any(result_id not in selected_real_ids for result_id in result_ids):
        raise ToolCallingEvaluationInputError("invalid_v2_report_scope")
    if complete and set(result_ids) != selected_real_ids:
        raise ToolCallingEvaluationInputError("incomplete_v2_report")

    case_by_id = {str(case["id"]): case for case in cases}
    excluded_cases = [
        {
            "id": case_id,
            "split": case_by_id[case_id]["split"],
            "category": case_by_id[case_id]["category"],
            "layer": contract.layer,
            "reason": contract.expected_error or "deterministic_contract",
            "evidence": list(contract.evidence),
        }
        for case_id, contract in manifest.items()
        if contract.layer == "deterministic_contract"
    ]
    metrics = compute_v2_metrics(ordered_results)
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "complete" if complete else "in_progress",
        "configuration": _redact_configuration(configuration),
        "fingerprint": fingerprint,
        "coverage": _coverage(cases, manifest),
        "run_scope": {
            "selected_split": selected_split,
            "selected_real_model_total": len(selected_real_ids),
            "selected_development_real_model_total": sum(
                case_by_id[case_id]["split"] == "development"
                for case_id in selected_real_ids
            ),
            "selected_holdout_real_model_total": sum(
                case_by_id[case_id]["split"] == "holdout"
                for case_id in selected_real_ids
            ),
        },
        "metrics": metrics,
        "metrics_by_split": compute_v2_metrics_by_split(ordered_results),
        "metrics_by_layer": {
            "real_model_flow": metrics,
            "deterministic_contract": {
                "sample_count": 0,
                "excluded_count": len(excluded_cases),
            },
        },
        "excluded_cases": excluded_cases,
        "results": ordered_results,
        "quality_gate": None,
    }


def load_v2_resumable_results(
    output: str | Path,
    *,
    configuration: dict[str, Any],
    fingerprint: dict[str, Any],
    allowed_case_ids: set[str],
    force_fresh: bool = False,
) -> dict[str, dict[str, Any]]:
    path = Path(output)
    if force_fresh or not path.exists():
        return {}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 2
        or report.get("configuration") != _redact_configuration(configuration)
        or report.get("fingerprint") != fingerprint
        or not isinstance(report.get("results"), list)
    ):
        return {}
    resumable: dict[str, dict[str, Any]] = {}
    for result in report["results"]:
        if not isinstance(result, dict):
            return {}
        case_id = result.get("id")
        if (
            not isinstance(case_id, str)
            or case_id not in allowed_case_ids
            or case_id in resumable
            or result.get("layer") != "real_model_flow"
        ):
            return {}
        resumable[case_id] = result
    return resumable


class V2EvaluationRunner:
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
            raise ToolCallingEvaluationInputError("invalid_v2_split")
        self._evaluate = evaluate
        self._registry = registry
        self._manifest = manifest
        self._configuration = configuration
        self._fingerprint = fingerprint
        self._force_fresh = force_fresh
        self._split = split

    def run(
        self,
        cases: Sequence[dict[str, Any]],
        output_path: str | Path,
    ) -> dict[str, Any]:
        case_list = list(cases)
        case_ids = [str(case.get("id", "")) for case in case_list]
        if (
            not case_list
            or any(not case_id for case_id in case_ids)
            or len(case_ids) != len(set(case_ids))
            or set(case_ids) != set(self._manifest)
        ):
            raise ToolCallingEvaluationInputError("invalid_v2_runner_catalog")
        case_by_id = {str(case["id"]): case for case in case_list}
        real_case_ids = [
            case_id
            for case_id in case_ids
            if self._manifest[case_id].layer == "real_model_flow"
            and (
                self._split == "all"
                or case_by_id[case_id]["split"] == self._split
            )
        ]
        output = Path(output_path)
        results_by_id = load_v2_resumable_results(
            output,
            configuration=self._configuration,
            fingerprint=self._fingerprint,
            allowed_case_ids=set(real_case_ids),
            force_fresh=self._force_fresh,
        )
        for case_id in real_case_ids:
            if case_id in results_by_id:
                continue
            case = case_by_id[case_id]
            contract = self._manifest[case_id]
            trace = self._evaluate(case, contract)
            if not isinstance(trace, V2FlowTrace):
                raise ToolCallingEvaluationInputError(
                    f"invalid_v2_trace:{case_id}"
                )
            results_by_id[case_id] = score_v2_case(
                case,
                contract,
                trace,
                registry=self._registry,
            )
            partial_results = [
                results_by_id[item_id]
                for item_id in real_case_ids
                if item_id in results_by_id
            ]
            _atomic_write_json(
                output,
                build_v2_report(
                    cases=case_list,
                    manifest=self._manifest,
                    results=partial_results,
                    configuration=self._configuration,
                    fingerprint=self._fingerprint,
                    complete=False,
                    selected_split=self._split,
                ),
            )
        ordered_results = [results_by_id[case_id] for case_id in real_case_ids]
        report = build_v2_report(
            cases=case_list,
            manifest=self._manifest,
            results=ordered_results,
            configuration=self._configuration,
            fingerprint=self._fingerprint,
            complete=True,
            selected_split=self._split,
        )
        _atomic_write_json(output, report)
        return report
