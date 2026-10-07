from __future__ import annotations

from collections import Counter
from inspect import signature
from pathlib import Path

import pytest

from policy_api.evaluation.procurement_holdout_generator import (
    generate_procurement_holdout,
    write_procurement_holdout,
)
from policy_api.evaluation.real_procurement_tool_flow import (
    ProcurementEvaluationFixtureStore,
    build_safe_procurement_evaluation_registry,
)
from policy_api.evaluation.tool_calling import load_tool_calling_cases
from policy_api.evaluation.tool_calling_v2 import load_v2_manifest


EXPECTED_DISTRIBUTION = {
    "holdout_policy": 3,
    "holdout_read": 3,
    "holdout_complete_submit": 5,
    "holdout_missing_submit_fields": 2,
    "holdout_business_rejection": 2,
    "holdout_withdraw_state": 2,
    "holdout_security": 3,
}


def test_generator_is_seeded_model_independent_and_has_exact_distribution() -> None:
    first_cases, first_manifest = generate_procurement_holdout("seed-a", count=20)
    repeated_cases, repeated_manifest = generate_procurement_holdout("seed-a", count=20)
    other_cases, _ = generate_procurement_holdout("seed-b", count=20)

    assert first_cases == repeated_cases
    assert first_manifest == repeated_manifest
    assert first_cases != other_cases
    assert len(first_cases) == 20
    assert Counter(case["category"] for case in first_cases) == EXPECTED_DISTRIBUTION
    assert all(case["split"] == "holdout" for case in first_cases)
    assert all(case["must_not_execute"] is True for case in first_cases)
    assert all(case["allowed_created_resource_count"] == 0 for case in first_cases)
    assert set(signature(generate_procurement_holdout).parameters) == {"seed", "count"}


def test_policy_queries_use_stable_safe_canonical_topics() -> None:
    first_cases, _ = generate_procurement_holdout("policy-contract-a", count=20)
    repeated_cases, _ = generate_procurement_holdout("policy-contract-a", count=20)
    other_cases, _ = generate_procurement_holdout("policy-contract-b", count=20)
    expected_topics = (
        "采购申请金额复审规则",
        "采购申请撤回允许状态",
        "采购审批代办与批量操作权限",
    )

    def policy_queries(cases: list[dict[str, object]]) -> tuple[object, ...]:
        return tuple(
            case["expected_arguments"]["query"]  # type: ignore[index]
            for case in cases
            if case["category"] == "holdout_policy"
        )

    assert policy_queries(first_cases) == expected_topics
    assert policy_queries(repeated_cases) == expected_topics
    assert policy_queries(other_cases) == expected_topics
    assert all(
        forbidden not in query
        for query in expected_topics
        for forbidden in ("由其他员工", "批量通过", "能否由")
    )


def test_generator_rejects_non_frozen_count() -> None:
    with pytest.raises(ValueError, match="procurement_holdout_count_must_be_20"):
        generate_procurement_holdout("seed-a", count=19)


def test_written_bundle_loads_with_production_contracts_and_never_overwrites(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.json"
    manifest_path = tmp_path / "manifest.json"

    metadata = write_procurement_holdout(
        seed="seed-c", count=20, cases_output=cases_path,
        manifest_output=manifest_path,
    )

    cases = load_tool_calling_cases(cases_path)
    registry = build_safe_procurement_evaluation_registry(ProcurementEvaluationFixtureStore())
    manifest = load_v2_manifest(manifest_path, cases=cases, registry=registry)
    assert len(cases) == len(manifest) == 20
    assert metadata["case_count"] == 20
    assert len(metadata["dataset_sha256"]) == 64
    assert len(metadata["manifest_sha256"]) == 64

    with pytest.raises(FileExistsError, match="holdout_output_exists"):
        write_procurement_holdout(
            seed="seed-d", count=20, cases_output=cases_path,
            manifest_output=manifest_path,
        )


def test_generated_expected_arguments_are_canonical_and_source_grounded() -> None:
    cases, manifest_payload = generate_procurement_holdout(
        "canonical-grounding", count=20
    )
    registry = build_safe_procurement_evaluation_registry(
        ProcurementEvaluationFixtureStore()
    )
    temporary_manifest = {
        case_id: contract for case_id, contract in manifest_payload.items()
    }

    for case in cases:
        contract = temporary_manifest[case["id"]]
        target = contract["target_tool"]
        expected = case["expected_arguments"]
        if target is None:
            assert expected == {}
        else:
            canonical = registry.get(target).input_model.model_validate(
                expected
            ).model_dump(mode="json", exclude_none=True)
            assert expected == canonical, case["id"]
        content = case["input_turns"][0]["content"]
        for field in ("comment", "reason"):
            value = expected.get(field)
            if isinstance(value, str):
                assert value in content, (case["id"], field)
        for item in expected.get("items", []):
            specification = item.get("specification")
            if isinstance(specification, str):
                assert specification in content, (case["id"], "specification")
            category_code = item.get("category_code")
            if isinstance(category_code, str):
                assert category_code in content, (case["id"], "category_code")
