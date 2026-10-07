from __future__ import annotations

import json
from pathlib import Path

import pytest

from policy_api.evaluation import real_tool_calling as real_tool_calling_module
from policy_api.evaluation.real_tool_calling import build_real_evaluation_registry
from policy_api.evaluation.tool_calling import (
    ToolCallingEvaluationInputError,
    load_tool_calling_cases,
)
from policy_api.evaluation.tool_calling_v2 import (
    V2FlowTrace,
    V2PlannedCall,
    build_v2_fingerprint,
    build_v2_report,
    compute_v2_metrics,
    compute_v2_metrics_by_split,
    load_v2_manifest,
    load_v2_resumable_results,
    metric_passes_at_least,
    score_v2_case,
)
from policy_api.models import UserRole
from policy_api.tools.orchestrator import SYSTEM_MESSAGE


ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "evaluation" / "hr_tool_calling_cases.json"
MANIFEST = ROOT / "evaluation" / "hr_tool_calling_case_manifest_v2.json"


def test_real_evaluator_fingerprint_bundle_covers_production_flow_files() -> None:
    assert hasattr(
        real_tool_calling_module,
        "EVALUATOR_IMPLEMENTATION_PATHS",
    )
    paths = real_tool_calling_module.EVALUATOR_IMPLEMENTATION_PATHS
    assert [path.name for path in paths] == [
        "orchestrator.py",
        "flow_policy.py",
        "registry.py",
        "planner_client.py",
        "tool_flow_policy.py",
        "tools.py",
        "real_tool_flow.py",
        "tool_calling_v2.py",
    ]
    bundle = real_tool_calling_module.build_evaluator_implementation_bundle()
    assert all(path.read_bytes() in bundle for path in paths)


def test_v2_manifest_exactly_partitions_the_fixed_catalog() -> None:
    cases = load_tool_calling_cases(DATASET)
    manifest = load_v2_manifest(
        MANIFEST,
        cases=cases,
        registry=build_real_evaluation_registry(),
    )

    assert len(manifest) == 60
    assert [
        item.case_id
        for item in manifest.values()
        if item.layer == "real_model_flow"
    ] == [
        *[f"HR-TC-{index:03d}" for index in range(1, 46)],
        "HR-TC-055",
        "HR-TC-056",
        "HR-TC-057",
    ]
    assert sum(
        item.layer == "deterministic_contract" for item in manifest.values()
    ) == 12


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_id",
        "unknown_id",
        "unknown_profile",
        "unknown_tool",
        "deterministic_without_evidence",
        "real_model_excluded",
        "target_not_required",
    ],
)
def test_v2_manifest_rejects_invalid_contract(
    mutation: str,
    tmp_path: Path,
) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if mutation == "missing_id":
        del payload["HR-TC-001"]
    elif mutation == "unknown_id":
        payload["HR-TC-999"] = payload.pop("HR-TC-001")
    elif mutation == "unknown_profile":
        payload["HR-TC-001"]["profile"] = "not_a_profile"
    elif mutation == "unknown_tool":
        payload["HR-TC-001"]["required_tools"] = ["hr.not_registered"]
    elif mutation == "deterministic_without_evidence":
        payload["HR-TC-046"]["evidence"] = []
    elif mutation == "real_model_excluded":
        payload["HR-TC-001"]["expected_terminal"] = "excluded"
    elif mutation == "target_not_required":
        payload["HR-TC-001"]["target_tool"] = "hr.list_my_leave_requests"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ToolCallingEvaluationInputError, match="invalid_v2_manifest"):
        load_v2_manifest(
            path,
            cases=load_tool_calling_cases(DATASET),
            registry=build_real_evaluation_registry(),
        )


def test_v2_manifest_uses_current_production_error_codes() -> None:
    manifest = load_v2_manifest(
        MANIFEST,
        cases=load_tool_calling_cases(DATASET),
        registry=build_real_evaluation_registry(),
    )

    assert {
        item.expected_error
        for item in manifest.values()
        if item.layer == "deterministic_contract"
    } == {
        None,
        "operation_id_conflict",
        "confirmation_expired",
        "confirmation_already_used",
        "confirmation_arguments_mismatch",
        "unknown_tool",
        "tool_write_limit_exceeded",
        "tool_model_call_limit_exceeded",
        "tool_provider_timeout",
        "tool_provider_unavailable",
        "tool_provider_rate_limited",
    }


def _case(case_id: str) -> dict[str, object]:
    return next(
        case for case in load_tool_calling_cases(DATASET) if case["id"] == case_id
    )


def _contract(case_id: str):
    return load_v2_manifest(
        MANIFEST,
        cases=load_tool_calling_cases(DATASET),
        registry=build_real_evaluation_registry(),
    )[case_id]


def test_complete_trace_allows_multiple_reads_before_one_write_proposal() -> None:
    case = _case("HR-TC-007")
    trace = V2FlowTrace(
        planned_calls=(
            V2PlannedCall("hr.get_my_leave_balances", {"year": 2026}),
            V2PlannedCall(
                "hr.calculate_leave_duration",
                {"start_date": "2026-09-07", "end_date": "2026-09-09"},
            ),
            V2PlannedCall("hr.submit_leave_request", case["expected_arguments"]),
        ),
        final_text=None,
        terminal_kind="write_proposal",
        terminal_error=None,
        model_call_count=2,
        read_call_count=2,
        write_proposal_count=1,
        latency_ms=2400,
        write_executed=False,
        created_resource_ids=(),
    )

    result = score_v2_case(
        case,
        _contract("HR-TC-007"),
        trace,
        registry=build_real_evaluation_registry(),
    )

    assert result["tool_selection_passed"] is True
    assert result["complete_parameters_passed"] is True
    assert result["terminal_outcome_passed"] is True
    assert result["must_not_execute_passed"] is True


def test_complete_trace_fails_missing_required_or_present_forbidden_tool() -> None:
    case = _case("HR-TC-007")
    trace = V2FlowTrace(
        planned_calls=(V2PlannedCall("hr.cancel_leave_request", {"request_id": 1}),),
        final_text="已处理",
        terminal_kind="text",
        terminal_error=None,
        model_call_count=1,
        read_call_count=0,
        write_proposal_count=1,
        latency_ms=10,
        write_executed=False,
        created_resource_ids=(),
    )

    result = score_v2_case(
        case,
        _contract("HR-TC-007"),
        trace,
        registry=build_real_evaluation_registry(),
    )

    assert result["tool_selection_passed"] is False
    assert result["complete_parameters_passed"] is False
    assert result["terminal_outcome_passed"] is False


def test_clarification_scores_final_text_with_closed_field_aliases() -> None:
    case = _case("HR-TC-010")
    trace = V2FlowTrace(
        planned_calls=(V2PlannedCall("hr.get_my_leave_balances", {}),),
        final_text="请补充请假类型、开始日期、结束日期和请假原因。",
        terminal_kind="clarification",
        terminal_error=None,
        model_call_count=2,
        read_call_count=1,
        write_proposal_count=0,
        latency_ms=100,
        write_executed=False,
        created_resource_ids=(),
    )

    result = score_v2_case(
        case,
        _contract("HR-TC-010"),
        trace,
        registry=build_real_evaluation_registry(),
    )

    assert result["clarification_fields"] == [
        "leave_type_code",
        "start_date",
        "end_date",
        "reason",
    ]
    assert result["clarification_passed"] is True
    assert result["complete_parameters_passed"] is None
    assert result["terminal_outcome_passed"] is True


def test_text_contract_is_not_failed_by_incidental_refusal_or_field_words() -> None:
    case = _case("HR-TC-028")
    trace = V2FlowTrace(
        planned_calls=(V2PlannedCall("hr.list_my_leave_requests", {}),),
        final_text="目前没有申请。若要新建，请提供请假类型和原因。",
        terminal_kind="clarification",
        terminal_error=None,
        model_call_count=2,
        read_call_count=1,
        write_proposal_count=0,
        latency_ms=100,
        write_executed=False,
        created_resource_ids=(),
    )

    result = score_v2_case(
        case,
        _contract("HR-TC-028"),
        trace,
        registry=build_real_evaluation_registry(),
    )

    assert result["tool_selection_passed"] is True
    assert result["terminal_outcome_passed"] is True


def test_safe_refusal_contract_accepts_nonwriting_owner_scoped_explanation() -> None:
    case = _case("HR-TC-040")
    trace = V2FlowTrace(
        planned_calls=(V2PlannedCall("hr.get_my_leave_balances", {}),),
        final_text=(
            "The tool only reads the current actor's balance and does not accept "
            "employee_id, so I did not access employee 9001."
        ),
        terminal_kind="text",
        terminal_error=None,
        model_call_count=2,
        read_call_count=1,
        write_proposal_count=0,
        latency_ms=100,
        write_executed=False,
        created_resource_ids=(),
    )

    result = score_v2_case(
        case,
        _contract("HR-TC-040"),
        trace,
        registry=build_real_evaluation_registry(),
    )

    assert result["tool_selection_passed"] is True
    assert result["terminal_outcome_passed"] is True
    assert result["must_not_execute_passed"] is True


def test_policy_query_parameters_allow_semantically_narrower_query_wording() -> None:
    case = _case("HR-TC-006")
    trace = V2FlowTrace(
        planned_calls=(
            V2PlannedCall(
                "knowledge.search_policy",
                {"query": "年假 结转 未休 规定"},
            ),
        ),
        final_text="制度规定如下。",
        terminal_kind="text",
        terminal_error=None,
        model_call_count=2,
        read_call_count=1,
        write_proposal_count=0,
        latency_ms=100,
        write_executed=False,
        created_resource_ids=(),
    )

    result = score_v2_case(
        case,
        _contract("HR-TC-006"),
        trace,
        registry=build_real_evaluation_registry(),
    )

    assert result["complete_parameters_passed"] is True
    assert result["terminal_outcome_passed"] is True


def test_clarification_scores_equivalent_english_field_names() -> None:
    case = _case("HR-TC-012")
    trace = V2FlowTrace(
        planned_calls=(),
        final_text="Please confirm the leave type and provide a brief reason.",
        terminal_kind="text",
        terminal_error=None,
        model_call_count=1,
        read_call_count=0,
        write_proposal_count=0,
        latency_ms=100,
        write_executed=False,
        created_resource_ids=(),
    )

    result = score_v2_case(
        case,
        _contract("HR-TC-012"),
        trace,
        registry=build_real_evaluation_registry(),
    )

    assert result["clarification_fields"] == ["leave_type_code", "reason"]
    assert result["clarification_passed"] is True
    assert result["terminal_outcome_passed"] is True


def test_domain_error_contract_accepts_safe_prevention_but_not_limit_error() -> None:
    case = _case("HR-TC-022")
    calls = (
        V2PlannedCall("hr.get_my_leave_balances", {"year": 2026}),
        V2PlannedCall(
            "hr.calculate_leave_duration",
            {"start_date": "2026-09-07", "end_date": "2026-09-11"},
        ),
    )
    safe = V2FlowTrace(
        planned_calls=calls,
        final_text="余额不足，不能提交这次申请。",
        terminal_kind="safe_refusal",
        terminal_error=None,
        model_call_count=2,
        read_call_count=2,
        write_proposal_count=0,
        latency_ms=100,
        write_executed=False,
        created_resource_ids=(),
    )
    limited = V2FlowTrace(
        planned_calls=calls,
        final_text=None,
        terminal_kind="error",
        terminal_error="tool_model_call_limit_exceeded",
        model_call_count=3,
        read_call_count=2,
        write_proposal_count=0,
        latency_ms=100,
        write_executed=False,
        created_resource_ids=(),
    )

    safe_result = score_v2_case(
        case,
        _contract("HR-TC-022"),
        safe,
        registry=build_real_evaluation_registry(),
    )
    limited_result = score_v2_case(
        case,
        _contract("HR-TC-022"),
        limited,
        registry=build_real_evaluation_registry(),
    )

    assert safe_result["tool_selection_passed"] is True
    assert safe_result["terminal_outcome_passed"] is True
    assert limited_result["tool_selection_passed"] is False
    assert limited_result["terminal_outcome_passed"] is False


def test_sunday_reference_date_resolves_next_monday_to_following_day() -> None:
    case = _case("HR-TC-013")

    assert case["expected_arguments"] == {
        "leave_type_code": "annual",
        "start_date": "2026-08-17",
        "end_date": "2026-08-19",
        "reason": "个人事务",
    }


def test_v2_metrics_exclude_contract_layer_and_keep_exact_split_counts() -> None:
    cases = load_tool_calling_cases(DATASET)
    manifest = load_v2_manifest(
        MANIFEST,
        cases=cases,
        registry=build_real_evaluation_registry(),
    )
    results = [
        {
            "id": case["id"],
            "split": case["split"],
            "layer": manifest[case["id"]].layer,
            "latency_ms": 5,
            "tool_selection_passed": True,
            "complete_parameters_passed": True,
            "clarification_passed": True,
            "terminal_outcome_passed": True,
            "must_not_execute_passed": True,
            "created_resource_ids": [],
            "allowed_created_resource_count": 0,
        }
        for case in cases
    ]

    overall = compute_v2_metrics(results)
    by_split = compute_v2_metrics_by_split(results)

    assert overall["sample_count"] == 48
    assert by_split["development"]["sample_count"] == 40
    assert by_split["holdout"]["sample_count"] == 8
    assert overall["terminal_outcome_accuracy"] == {
        "numerator": 48,
        "denominator": 48,
        "value": 1.0,
    }


def test_zero_denominator_metric_is_null_and_fails_closed() -> None:
    metrics = compute_v2_metrics(
        [
            {
                "id": "HR-TC-001",
                "split": "development",
                "layer": "real_model_flow",
                "latency_ms": 5,
                "tool_selection_passed": True,
                "complete_parameters_passed": None,
                "clarification_passed": None,
                "terminal_outcome_passed": True,
                "must_not_execute_passed": True,
                "created_resource_ids": [],
                "allowed_created_resource_count": 0,
            }
        ]
    )

    assert metrics["clarification_accuracy"]["value"] is None
    assert metric_passes_at_least(metrics["clarification_accuracy"], 0.95) is False


def _fingerprint(**overrides: object) -> dict[str, object]:
    registry = build_real_evaluation_registry()
    values = {
        "cases": load_tool_calling_cases(DATASET),
        "manifest_payload": json.loads(MANIFEST.read_text(encoding="utf-8")),
        "system_message": SYSTEM_MESSAGE,
        "provider_tools": registry.provider_tools(role=UserRole.EMPLOYEE),
        "model": "deepseek-v4-flash",
        "reference_date": "2026-08-16",
        "orchestrator_limits": {"model": 3, "read": 4, "write": 1},
        "evaluator_implementation": "version-a",
    }
    values.update(overrides)
    return build_v2_fingerprint(**values)


def test_v2_fingerprint_covers_every_semantic_evaluation_input() -> None:
    fingerprint = _fingerprint()

    assert set(fingerprint) == {
        "dataset_sha256",
        "manifest_sha256",
        "evaluator_schema_version",
        "evaluator_implementation_sha256",
        "system_message_sha256",
        "provider_tool_schema_sha256",
        "model",
        "reference_date",
        "orchestrator_limits",
    }
    assert fingerprint["evaluator_schema_version"] == 2
    assert fingerprint["orchestrator_limits"] == {
        "model": 3,
        "read": 4,
        "write": 1,
    }


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("cases", [{"id": "changed"}]),
        ("manifest_payload", {"HR-TC-001": {"changed": True}}),
        ("system_message", SYSTEM_MESSAGE + " changed"),
        ("provider_tools", ({"type": "function", "function": {}},)),
        ("model", "deepseek-chat"),
        ("reference_date", "2026-08-17"),
        ("orchestrator_limits", {"model": 4, "read": 4, "write": 1}),
        ("evaluator_implementation", "version-b"),
    ],
)
def test_semantic_change_alters_v2_fingerprint(field: str, changed: object) -> None:
    assert _fingerprint(**{field: changed}) != _fingerprint()


def test_v2_report_has_exact_coverage_exclusions_and_redacted_configuration() -> None:
    cases = load_tool_calling_cases(DATASET)
    manifest = load_v2_manifest(
        MANIFEST,
        cases=cases,
        registry=build_real_evaluation_registry(),
    )
    report = build_v2_report(
        cases=cases,
        manifest=manifest,
        results=[],
        configuration={
            "mode": "real_model_full_turn",
            "api_key": "hidden",
            "nested": {"authorization": "hidden", "safe": "kept"},
        },
        fingerprint=_fingerprint(),
        complete=False,
    )

    assert report["schema_version"] == 2
    assert report["status"] == "in_progress"
    assert report["coverage"] == {
        "catalog_total": 60,
        "real_model_total": 48,
        "deterministic_contract_total": 12,
        "development_real_model_total": 40,
        "holdout_real_model_total": 8,
    }
    assert len(report["excluded_cases"]) == 12
    assert report["configuration"] == {
        "mode": "real_model_full_turn",
        "nested": {"safe": "kept"},
    }
    assert "hidden" not in json.dumps(report, ensure_ascii=False)


def test_v2_resume_requires_exact_configuration_and_fingerprint(
    tmp_path: Path,
) -> None:
    cases = load_tool_calling_cases(DATASET)
    manifest = load_v2_manifest(
        MANIFEST,
        cases=cases,
        registry=build_real_evaluation_registry(),
    )
    result = {
        "id": "HR-TC-001",
        "split": "development",
        "layer": "real_model_flow",
        "latency_ms": 5,
        "tool_selection_passed": True,
        "complete_parameters_passed": True,
        "clarification_passed": None,
        "terminal_outcome_passed": True,
        "must_not_execute_passed": True,
        "created_resource_ids": [],
        "allowed_created_resource_count": 0,
    }
    configuration = {"mode": "real_model_full_turn"}
    report = build_v2_report(
        cases=cases,
        manifest=manifest,
        results=[result],
        configuration=configuration,
        fingerprint=_fingerprint(),
        complete=False,
    )
    output = tmp_path / "partial.json"
    output.write_text(json.dumps(report), encoding="utf-8")
    allowed = {
        case_id
        for case_id, contract in manifest.items()
        if contract.layer == "real_model_flow"
    }

    assert set(
        load_v2_resumable_results(
            output,
            configuration=configuration,
            fingerprint=_fingerprint(),
            allowed_case_ids=allowed,
        )
    ) == {"HR-TC-001"}
    assert load_v2_resumable_results(
        output,
        configuration={"mode": "changed"},
        fingerprint=_fingerprint(),
        allowed_case_ids=allowed,
    ) == {}
    assert load_v2_resumable_results(
        output,
        configuration=configuration,
        fingerprint=_fingerprint(system_message=SYSTEM_MESSAGE + " changed"),
        allowed_case_ids=allowed,
    ) == {}
    assert load_v2_resumable_results(
        output,
        configuration=configuration,
        fingerprint=_fingerprint(),
        allowed_case_ids=allowed,
        force_fresh=True,
    ) == {}
