from __future__ import annotations

import math
import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ToolCallingEvaluationInputError(ValueError):
    pass


_REQUIRED_CASE_FIELDS = {
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
_SECRET_KEY_PARTS = ("api_key", "authorization", "password", "secret", "token")


def _metric(values: Sequence[bool | None]) -> dict[str, int | float | None]:
    applicable = [value for value in values if value is not None]
    numerator = sum(value is True for value in applicable)
    denominator = len(applicable)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(numerator / denominator, 4) if denominator else None,
    }


def compute_tool_calling_metrics(
    results: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    if not results:
        raise ToolCallingEvaluationInputError("empty_results")
    latencies = sorted(int(item["latency_ms"]) for item in results)
    p95 = latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)]
    duplicate_resources = sum(
        max(
            0,
            len(set(str(value) for value in item.get("created_resource_ids", [])))
            - int(item.get("allowed_created_resource_count", 0)),
        )
        for item in results
    )
    return {
        "sample_count": len(results),
        "tool_selection_accuracy": _metric(
            [bool(item["tool_selection_passed"]) for item in results]
        ),
        "complete_parameter_accuracy": _metric(
            [item.get("complete_parameters_passed") for item in results]
        ),
        "clarification_accuracy": _metric(
            [item.get("clarification_passed") for item in results]
        ),
        "must_not_execute_accuracy": _metric(
            [item.get("must_not_execute_passed") for item in results]
        ),
        "duplicate_resource_count": duplicate_resources,
        "p95_latency_ms": p95,
    }


def load_tool_calling_cases(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolCallingEvaluationInputError("invalid_case_file") from exc
    if not isinstance(payload, list) or not payload:
        raise ToolCallingEvaluationInputError("invalid_case_collection")

    seen_ids: set[str] = set()
    for index, case in enumerate(payload):
        if not isinstance(case, dict) or not _REQUIRED_CASE_FIELDS <= set(case):
            raise ToolCallingEvaluationInputError(f"invalid_case:{index}")
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id or case_id in seen_ids:
            raise ToolCallingEvaluationInputError(f"invalid_case_id:{index}")
        seen_ids.add(case_id)
        if case["split"] not in {"development", "holdout"}:
            raise ToolCallingEvaluationInputError(f"invalid_split:{case_id}")
        if case["parameter_expectation"] not in {
            "complete",
            "clarification",
            "not_applicable",
        }:
            raise ToolCallingEvaluationInputError(
                f"invalid_parameter_expectation:{case_id}"
            )
        if not isinstance(case["input_turns"], list) or not case["input_turns"]:
            raise ToolCallingEvaluationInputError(f"invalid_input_turns:{case_id}")
        if not isinstance(case["expected_arguments"], dict):
            raise ToolCallingEvaluationInputError(f"invalid_expected_arguments:{case_id}")
        if "employee_id" in case["expected_arguments"]:
            raise ToolCallingEvaluationInputError(f"actor_field_forbidden:{case_id}")
        if case["allowed_created_resource_count"] not in {0, 1}:
            raise ToolCallingEvaluationInputError(
                f"invalid_allowed_created_resource_count:{case_id}"
            )
    return payload


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redact_configuration(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_configuration(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SECRET_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_redact_configuration(item) for item in value]
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _matches_clarification(actual: Any, expected: Any) -> bool:
    if not isinstance(actual, list) or not isinstance(expected, list):
        return actual == expected
    return sorted(str(value) for value in actual) == sorted(
        str(value) for value in expected
    )


class ToolCallingEvaluationRunner:
    def __init__(
        self,
        *,
        evaluate: Any,
        configuration: dict[str, Any],
    ) -> None:
        if not callable(evaluate):
            raise ToolCallingEvaluationInputError("evaluate_not_callable")
        self._evaluate = evaluate
        self._configuration = _redact_configuration(configuration)

    def run(
        self,
        cases: Sequence[dict[str, Any]],
        output_path: str | Path,
    ) -> dict[str, Any]:
        if not cases:
            raise ToolCallingEvaluationInputError("empty_cases")
        case_list = list(cases)
        case_ids = [str(case.get("id", "")) for case in case_list]
        if any(not case_id for case_id in case_ids) or len(set(case_ids)) != len(case_ids):
            raise ToolCallingEvaluationInputError("invalid_case_ids")

        output = Path(output_path)
        case_set_sha256 = _canonical_sha256(case_list)
        results_by_id = self._load_resumable_results(
            output,
            case_set_sha256=case_set_sha256,
            allowed_case_ids=set(case_ids),
        )

        for case in case_list:
            case_id = str(case["id"])
            if case_id in results_by_id:
                continue
            observation = self._evaluate(case)
            if not isinstance(observation, dict):
                raise ToolCallingEvaluationInputError(
                    f"invalid_observation:{case_id}"
                )
            results_by_id[case_id] = self._score_case(case, observation)
            partial_results = [
                results_by_id[item_id]
                for item_id in case_ids
                if item_id in results_by_id
            ]
            _atomic_write_json(
                output,
                self._build_report(
                    partial_results,
                    case_set_sha256=case_set_sha256,
                    complete=len(partial_results) == len(case_list),
                ),
            )

        ordered_results = [results_by_id[case_id] for case_id in case_ids]
        report = self._build_report(
            ordered_results,
            case_set_sha256=case_set_sha256,
            complete=True,
        )
        _atomic_write_json(output, report)
        return report

    def _load_resumable_results(
        self,
        output: Path,
        *,
        case_set_sha256: str,
        allowed_case_ids: set[str],
    ) -> dict[str, dict[str, Any]]:
        if not output.exists():
            return {}
        try:
            report = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if (
            not isinstance(report, dict)
            or report.get("case_set_sha256") != case_set_sha256
            or report.get("configuration") != self._configuration
            or not isinstance(report.get("results"), list)
        ):
            return {}
        resumable: dict[str, dict[str, Any]] = {}
        for result in report["results"]:
            if not isinstance(result, dict):
                return {}
            case_id = result.get("id")
            if case_id not in allowed_case_ids or case_id in resumable:
                return {}
            resumable[str(case_id)] = result
        return resumable

    def _score_case(
        self,
        case: dict[str, Any],
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        parameter_expectation = case["parameter_expectation"]
        created_resource_ids = [
            str(value) for value in observation.get("created_resource_ids", [])
        ]
        write_executed = bool(observation.get("write_executed", False))
        return {
            "id": case["id"],
            "split": case["split"],
            "category": case["category"],
            "selected_tool": observation.get("selected_tool"),
            "normalized_arguments": observation.get("normalized_arguments", {}),
            "clarification_fields": observation.get("clarification_fields", []),
            "error_code": observation.get("error_code"),
            "write_proposed": bool(observation.get("write_proposed", False)),
            "write_executed": write_executed,
            "created_resource_ids": created_resource_ids,
            "allowed_created_resource_count": case["allowed_created_resource_count"],
            "latency_ms": int(observation.get("latency_ms", 0)),
            "tool_selection_passed": (
                observation.get("selected_tool") == case["expected_tool"]
            ),
            "complete_parameters_passed": (
                observation.get("normalized_arguments", {})
                == case["expected_arguments"]
                if parameter_expectation == "complete"
                else None
            ),
            "clarification_passed": (
                _matches_clarification(
                    observation.get("clarification_fields", []),
                    case["expected_clarification"],
                )
                if parameter_expectation == "clarification"
                else None
            ),
            "must_not_execute_passed": (
                not write_executed and not created_resource_ids
                if case["must_not_execute"]
                else None
            ),
            "error_passed": observation.get("error_code") == case["expected_error"],
            "write_proposal_passed": (
                bool(observation.get("write_proposed", False))
                == bool(case["allow_write_proposal"])
            ),
        }

    def _build_report(
        self,
        results: list[dict[str, Any]],
        *,
        case_set_sha256: str,
        complete: bool,
    ) -> dict[str, Any]:
        metrics_by_split = {
            split: compute_tool_calling_metrics(
                [result for result in results if result["split"] == split]
            )
            for split in ("development", "holdout")
            if any(result["split"] == split for result in results)
        }
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "configuration": self._configuration,
            "case_set_sha256": case_set_sha256,
            "status": "complete" if complete else "in_progress",
            "metrics": compute_tool_calling_metrics(results),
            "metrics_by_split": metrics_by_split,
            "results": results,
        }
