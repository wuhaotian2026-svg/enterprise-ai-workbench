from __future__ import annotations

import ast
import json
from pathlib import Path

from policy_api.evaluation.slot_extraction import load_slot_extraction_catalog
from policy_api.evaluation.slot_extraction_holdout_generator import (
    EXPECTED_HOLDOUT_DISTRIBUTION,
    PREDECLARED_FAMILIES,
    generate_holdout,
    write_holdout,
)


def test_same_seed_produces_identical_cases_and_manifest() -> None:
    first = generate_holdout(seed="seed-a", reference_date="2026-08-28")
    second = generate_holdout(seed="seed-a", reference_date="2026-08-28")
    assert first.cases_payload == second.cases_payload
    assert first.manifest_payload == second.manifest_payload
    assert first.case_sha256 == second.case_sha256
    assert first.manifest_sha256 == second.manifest_sha256


def test_different_seed_changes_case_hash() -> None:
    assert generate_holdout(
        seed="seed-a", reference_date="2026-08-28"
    ).case_sha256 != generate_holdout(
        seed="seed-b", reference_date="2026-08-28"
    ).case_sha256


def test_holdout_has_exact_module_and_category_distribution() -> None:
    result = generate_holdout(
        seed="distribution-seed", reference_date="2026-08-28"
    )
    assert result.distribution == EXPECTED_HOLDOUT_DISTRIBUTION
    assert len(result.cases) == 20


def test_ambiguity_metric_only_applies_to_preservation_scenarios() -> None:
    result = generate_holdout(
        seed="metric-applicability-seed",
        reference_date="2026-08-28",
    )

    assert all(
        ("ambiguity_handled" in expectation.applicable_metrics)
        == (expectation.category == "ambiguity_conflict")
        for expectation in result.manifest
    )


def test_generator_uses_only_predeclared_scenario_families() -> None:
    result = generate_holdout(seed="family-seed", reference_date="2026-08-28")
    assert set(result.family_ids) <= set(PREDECLARED_FAMILIES)
    assert len(result.family_ids) == 20


def test_holdout_covers_bounded_natural_forms_and_partial_item_contracts() -> None:
    result = generate_holdout(
        seed="contract-coverage-seed",
        reference_date="2026-08-28",
    )
    by_family = dict(zip(result.family_ids, result.cases, strict=True))
    manifest_by_id = {item.case_id: item for item in result.manifest}
    serialized_turns = "\n".join(case.current_user_turn for case in result.cases)

    assert "号" in serialized_turns
    assert "就是2031年" in serialized_turns
    assert "六百块" in serialized_turns

    mixed = by_family["procurement_natural_office"]
    mixed_expected = manifest_by_id[mixed.id]
    assert mixed_expected.expected_pending == {"items": "item_fields_required"}
    assert mixed_expected.expected_clarification_fields == [
        "items[1].estimated_unit_price"
    ]

    followup = by_family["procurement_followup_item"]
    followup_expected = manifest_by_id[followup.id]
    assert followup.initial_pending["items"]["canonical_fragment"]["items"][0][  # type: ignore[index]
        "fields"
    ] == {
        "item_name": "待补全物品",
        "quantity": "1",
        "estimated_unit_price": "600",
    }
    assert followup_expected.expected_terminal == "accepted"
    assert followup_expected.expected_clarification_fields == []


def test_generated_complete_procurement_items_always_have_explicit_category_text() -> None:
    result = generate_holdout(
        seed="category-contract-seed",
        reference_date="2026-08-28",
    )
    cases_by_id = {case.id: case for case in result.cases}
    category_phrases = ("办公用品类", "IT设备类", "专业服务类")
    for expected in result.manifest:
        if expected.module != "procurement" or "items" not in expected.expected_fields:
            continue
        assert any(
            phrase in cases_by_id[expected.case_id].current_user_turn
            for phrase in category_phrases
        )


def test_generator_does_not_import_reports_provider_outputs_or_data_sources() -> None:
    import policy_api.evaluation.slot_extraction_holdout_generator as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden_parts = {
        "real_slot_extraction", "report", "provider", "sqlalchemy", "database"
    }
    assert not {
        name for name in imports if any(part in name for part in forbidden_parts)
    }
    assert ".env" not in source


def test_generated_values_are_fictitious_and_non_production() -> None:
    result = generate_holdout(seed="safety-seed", reference_date="2026-08-28")
    serialized = json.dumps(result.cases_payload["cases"], ensure_ascii=False)
    assert all(case.id.startswith("SE-HOLDOUT-") for case in result.cases)
    assert "localhost" not in serialized
    assert "@" not in serialized
    assert "deepseek" not in serialized.lower()
    assert "2026-08-28" not in serialized


def test_written_holdout_is_scorer_compatible_and_never_overwrites(
    tmp_path: Path,
) -> None:
    cases_path = tmp_path / "cases.json"
    manifest_path = tmp_path / "manifest.json"
    generated = write_holdout(
        seed="writer-seed",
        reference_date="2026-08-28",
        cases_output=cases_path,
        manifest_output=manifest_path,
    )

    cases, manifest = load_slot_extraction_catalog(cases_path, manifest_path)
    assert len(cases) == len(manifest) == 20
    assert generated.case_sha256 == json.loads(
        cases_path.read_text(encoding="utf-8")
    )["case_sha256"]

    try:
        write_holdout(
            seed="writer-seed",
            reference_date="2026-08-28",
            cases_output=cases_path,
            manifest_output=manifest_path,
        )
    except ValueError as exc:
        assert str(exc) == "slot_extraction_holdout_output_exists"
    else:
        raise AssertionError("holdout generator must never overwrite prior output")
