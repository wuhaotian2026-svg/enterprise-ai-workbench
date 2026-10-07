from __future__ import annotations

import json
from pathlib import Path

import pytest

from policy_api.answers.prompt import sanitize_untrusted_question
from policy_api.evaluation.rag_holdout import (
    RagHoldoutError,
    generate_rag_seed_blind_holdout,
    write_rag_seed_blind_holdout,
)
from policy_api.evaluation.rag_v2 import load_rag_v2_cases, load_rag_v2_manifest


def test_holdout_is_seeded_model_independent_and_covers_every_safety_family() -> None:
    first_cases, first_manifest = generate_rag_seed_blind_holdout(2026081801)
    repeated_cases, repeated_manifest = generate_rag_seed_blind_holdout(2026081801)
    other_cases, _ = generate_rag_seed_blind_holdout(2026081802)

    assert first_cases == repeated_cases
    assert first_manifest == repeated_manifest
    assert first_cases != other_cases
    assert len(first_cases) == 12
    assert len({case["id"] for case in first_cases}) == 12
    assert set(first_manifest) == {case["id"] for case in first_cases}
    assert {case["expected_status"] for case in first_cases} == {
        "answered",
        "needs_clarification",
        "abstained",
    }
    tags = {tag for case in first_cases for tag in case["tags"]}
    assert {
        "specific_scenario",
        "explicit_negative_rule",
        "prompt_injection",
        "disabled_document",
    } <= tags
    assert "deepseek" not in repr(first_cases).lower()
    assert "model_output" not in repr(first_cases).lower()
    assert not {"南京", "月球", "火星"} & {
        location
        for location in ("南京", "月球", "火星")
        if location in repr(first_cases)
    }

    procurement = next(
        case
        for case in first_cases
        if case["category"] == "procurement" and "软件" in case["question"]
    )
    if "12800元" in procurement["question"]:
        assert procurement["expected_facts"] == [
            "发起采购申请",
            "分管负责人批准",
            "至少3家有效报价",
        ]
    else:
        assert procurement["expected_facts"] == [
            "发起采购申请",
            "部门负责人批准",
        ]


def test_holdout_bundle_uses_exclusive_create_and_records_hashes(tmp_path: Path) -> None:
    dataset = tmp_path / "rag-holdout.jsonl"
    manifest = tmp_path / "rag-holdout-manifest.json"
    metadata = tmp_path / "rag-holdout-metadata.json"

    result = write_rag_seed_blind_holdout(
        seed=2026081803,
        dataset_path=dataset,
        manifest_path=manifest,
        metadata_path=metadata,
    )

    cases = load_rag_v2_cases(dataset)
    contracts = load_rag_v2_manifest(manifest, cases=cases)
    persisted = json.loads(metadata.read_text(encoding="utf-8"))
    assert len(cases) == len(contracts) == 12
    assert result == persisted
    assert result["seed"] == 2026081803
    assert result["case_count"] == 12
    assert len(result["dataset_sha256"]) == 64
    assert len(result["manifest_sha256"]) == 64

    with pytest.raises(RagHoldoutError, match="holdout_output_exists"):
        write_rag_seed_blind_holdout(
            seed=2026081804,
            dataset_path=dataset,
            manifest_path=manifest,
            metadata_path=metadata,
        )


def test_prompt_injection_case_preserves_independent_safe_business_intent() -> None:
    cases, _ = generate_rag_seed_blind_holdout(2026081805)
    injection_case = next(
        case for case in cases if "prompt_injection" in case["tags"]
    )

    safe_question = sanitize_untrusted_question(injection_case["question"])

    assert safe_question != "未提供可安全处理的企业制度业务问题"
    assert "忽略" not in safe_question
    assert "网上" not in safe_question
    assert "公司制度" in safe_question
    assert "差旅" in safe_question or "出差" in safe_question
    assert "计算" in safe_question or "多少" in safe_question
