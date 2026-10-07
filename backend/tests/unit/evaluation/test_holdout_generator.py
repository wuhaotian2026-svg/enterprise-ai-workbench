from __future__ import annotations

import re
from collections import Counter
from uuid import UUID

import pytest

from policy_api.evaluation.holdout_generator import (
    generate_seed_blind_holdout,
    write_seed_blind_holdout,
)
from policy_api.evaluation.real_tool_flow import (
    EvaluationFixtureStore,
    build_safe_evaluation_registry,
)
from policy_api.evaluation.tool_calling import load_tool_calling_cases
from policy_api.evaluation.tool_calling_v2 import load_v2_manifest


EXPECTED_DISTRIBUTION = {
    "holdout_policy": 3,
    "holdout_read": 3,
    "holdout_complete_submit": 5,
    "holdout_clarification": 2,
    "holdout_business_guard": 2,
    "holdout_cancel": 2,
    "holdout_security": 3,
}


def test_holdout_generator_is_seeded_and_has_the_frozen_distribution() -> None:
    first_cases, first_manifest = generate_seed_blind_holdout(2026081801)
    repeated_cases, repeated_manifest = generate_seed_blind_holdout(2026081801)
    other_cases, _other_manifest = generate_seed_blind_holdout(2026081802)

    assert first_cases == repeated_cases
    assert first_manifest == repeated_manifest
    assert first_cases != other_cases
    assert len(first_cases) == 20
    assert len({case["id"] for case in first_cases}) == 20
    assert Counter(case["category"] for case in first_cases) == EXPECTED_DISTRIBUTION
    assert all(case["split"] == "holdout" for case in first_cases)
    assert all(contract["layer"] == "real_model_flow" for contract in first_manifest.values())
    assert set(first_manifest) == {case["id"] for case in first_cases}


def test_holdout_uses_fictional_ids_non_production_dates_and_canonical_types() -> None:
    cases, _manifest = generate_seed_blind_holdout(2026081803)
    serialized = repr(cases)
    uuids = [UUID(value) for value in re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        serialized,
    )]

    assert uuids
    assert all(value.version == 4 for value in uuids)
    assert "2026-" not in serialized
    assert "2027-" in serialized
    assert "comp_time" not in serialized
    for case in cases:
        leave_type = case["expected_arguments"].get("leave_type_code")
        assert leave_type in {None, "annual", "compensatory"}


def test_written_holdout_bundle_loads_with_production_contracts(tmp_path) -> None:
    dataset = tmp_path / "blind-cases.json"
    manifest = tmp_path / "blind-manifest.json"
    metadata = tmp_path / "blind-metadata.json"

    result = write_seed_blind_holdout(
        seed=2026081804,
        dataset_path=dataset,
        manifest_path=manifest,
        metadata_path=metadata,
    )

    loaded_cases = load_tool_calling_cases(dataset)
    registry = build_safe_evaluation_registry(EvaluationFixtureStore())
    loaded_manifest = load_v2_manifest(
        manifest,
        cases=loaded_cases,
        registry=registry,
    )
    assert len(loaded_cases) == 20
    assert len(loaded_manifest) == 20
    assert result["seed"] == 2026081804
    assert result["case_count"] == 20
    assert len(result["dataset_sha256"]) == 64
    assert len(result["manifest_sha256"]) == 64
    assert metadata.exists()

    with pytest.raises(FileExistsError, match="holdout_output_exists"):
        write_seed_blind_holdout(
            seed=2026081805,
            dataset_path=dataset,
            manifest_path=manifest,
            metadata_path=metadata,
        )
