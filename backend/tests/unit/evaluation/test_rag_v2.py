from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from policy_api.evaluation.rag_v2 import (
    IMPLEMENTATION_PATHS,
    EvaluationFingerprintMismatch,
    RagV2InputError,
    RagV2Trace,
    build_rag_v2_report,
    build_rag_v2_fingerprint,
    load_rag_v2_cases,
    load_rag_v2_manifest,
    load_rag_v2_resumable_results,
    rag_v2_quality_gate,
    score_rag_v2_case,
)


def test_evaluator_runner_is_part_of_implementation_fingerprint_bundle() -> None:
    assert "backend/src/policy_api/evaluation/runner.py" in IMPLEMENTATION_PATHS
from policy_api.evaluation.metrics import compute_rag_v2_metrics


ROOT = Path(__file__).resolve().parents[4]
DATASET = ROOT / "sample-data" / "evaluation" / "rag_v2_public_cases.jsonl"
MANIFEST = ROOT / "sample-data" / "evaluation" / "rag_v2_public_manifest.json"


def test_public_catalog_has_the_approved_22_specific_scenarios() -> None:
    cases = load_rag_v2_cases(DATASET)
    manifest = load_rag_v2_manifest(MANIFEST, cases=cases)

    assert len(cases) == 22
    assert len({case["id"] for case in cases}) == 22
    assert Counter(case["category"] for case in cases) == {
        "travel": 7,
        "leave": 6,
        "expense": 4,
        "procurement": 5,
    }
    assert {case["expected_status"] for case in cases} == {
        "answered",
        "needs_clarification",
        "abstained",
    }
    tags = {tag for case in cases for tag in case["tags"]}
    assert {
        "specific_scenario",
        "explicit_negative_rule",
        "prompt_injection",
        "disabled_document",
        "cross_section",
    } <= tags
    assert set(manifest) == {case["id"] for case in cases}
    for case in cases:
        contract = manifest[case["id"]]
        assert contract.expected_status == case["expected_status"]
        assert isinstance(contract.required_clarification_topics, tuple)
        assert len(contract.required_clarification_topics) <= 3
        assert isinstance(contract.must_not_assert, tuple)
        assert isinstance(contract.must_not_retrieve_disabled, bool)


@pytest.mark.parametrize(
    "topics",
    [[], ["城市档位", "住宿晚数", "全天供餐", "展会超标审批"]],
)
def test_manifest_enforces_one_to_three_topics_for_clarification(
    tmp_path: Path,
    topics: list[str],
) -> None:
    cases = load_rag_v2_cases(DATASET)
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["RAG-V2-001"]["required_clarification_topics"] = topics
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RagV2InputError, match="invalid_rag_v2_manifest"):
        load_rag_v2_manifest(path, cases=cases)


def test_rag_v2_metrics_keep_exact_numerators_denominators_and_latencies() -> None:
    results = [
        {
            "retrieval_recall_at_5_passed": True,
            "specific_terminal_correctness_passed": True,
            "answer_correctness_passed": True,
            "clarification_precision_passed": None,
            "clarification_recall_passed": None,
            "citation_support_passed": True,
            "citation_validation_passed": True,
            "correct_abstention_passed": None,
            "disabled_retrieval_violation": False,
            "unsupported_definitive_answer": False,
            "no_evidence_conflict_must_abstain_passed": None,
            "retrieval_latency_ms": 100,
            "end_to_end_latency_ms": 1000,
        },
        {
            "retrieval_recall_at_5_passed": False,
            "specific_terminal_correctness_passed": True,
            "answer_correctness_passed": None,
            "clarification_precision_passed": True,
            "clarification_recall_passed": False,
            "citation_support_passed": True,
            "citation_validation_passed": True,
            "correct_abstention_passed": None,
            "disabled_retrieval_violation": False,
            "unsupported_definitive_answer": False,
            "no_evidence_conflict_must_abstain_passed": None,
            "retrieval_latency_ms": 200,
            "end_to_end_latency_ms": 2000,
        },
        {
            "retrieval_recall_at_5_passed": None,
            "specific_terminal_correctness_passed": False,
            "answer_correctness_passed": None,
            "clarification_precision_passed": None,
            "clarification_recall_passed": None,
            "citation_support_passed": None,
            "citation_validation_passed": True,
            "correct_abstention_passed": True,
            "disabled_retrieval_violation": True,
            "unsupported_definitive_answer": False,
            "no_evidence_conflict_must_abstain_passed": True,
            "retrieval_latency_ms": 300,
            "end_to_end_latency_ms": 3000,
        },
    ]

    metrics = compute_rag_v2_metrics(results)

    assert metrics["retrieval_recall_at_5"] == {
        "numerator": 1,
        "denominator": 2,
        "value": 0.5,
    }
    assert metrics["specific_terminal_correctness"] == {
        "numerator": 2,
        "denominator": 3,
        "value": 0.6667,
    }
    assert metrics["clarification_precision"] == {
        "numerator": 1,
        "denominator": 1,
        "value": 1.0,
    }
    assert metrics["clarification_recall"] == {
        "numerator": 0,
        "denominator": 1,
        "value": 0.0,
    }
    assert metrics["disabled_retrieval_violations"] == {
        "numerator": 1,
        "denominator": 3,
        "value": 0.3333,
    }
    assert metrics["retrieval_p95_ms"] == {
        "numerator": 300,
        "denominator": 3,
        "value": 300,
    }
    assert metrics["end_to_end_p95_ms"] == {
        "numerator": 3000,
        "denominator": 3,
        "value": 3000,
    }


def test_zero_denominators_are_null_and_quality_gate_fails_closed() -> None:
    metrics = compute_rag_v2_metrics(
        [
            {
                "retrieval_recall_at_5_passed": None,
                "specific_terminal_correctness_passed": True,
                "answer_correctness_passed": None,
                "clarification_precision_passed": None,
                "clarification_recall_passed": None,
                "citation_support_passed": None,
                "citation_validation_passed": True,
                "correct_abstention_passed": None,
                "disabled_retrieval_violation": False,
                "unsupported_definitive_answer": False,
                "no_evidence_conflict_must_abstain_passed": None,
                "retrieval_latency_ms": 1,
                "end_to_end_latency_ms": 1,
            }
        ]
    )

    assert metrics["clarification_precision"] == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }
    gate = rag_v2_quality_gate(metrics)
    assert gate["passed"] is False
    assert "clarification_precision" in gate["failed_metrics"]

    empty = compute_rag_v2_metrics([])
    assert empty["disabled_retrieval_violations"] == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }
    assert empty["unsupported_definitive_answers"] == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }


def _fingerprint(**overrides: object) -> dict[str, object]:
    cases = load_rag_v2_cases(DATASET)
    values: dict[str, object] = {
        "cases": cases,
        "manifest_payload": json.loads(MANIFEST.read_text(encoding="utf-8")),
        "provider_schema": {"status": ["answered", "needs_clarification", "abstained"]},
        "chat_model": "deepseek-v4-flash",
        "embedding_model": "multilingual-e5-small",
        "retrieval_settings": {
            "query_variant_max_count": 3,
            "retrieval_lexical_candidate_limit": 20,
            "retrieval_vector_candidate_limit": 20,
            "retrieval_fused_top_k": 5,
            "retrieval_allow_vector_only_fallback": False,
            "evidence_semantic_threshold": 0.7,
            "evidence_lexical_threshold": 0.2,
            "evidence_dual_channel_minimum": 0.1,
            "evidence_max_chunks": 5,
        },
        "implementation_overrides": {"rag_v2.py": "version-a"},
    }
    values.update(overrides)
    return build_rag_v2_fingerprint(**values)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("cases", [{"id": "changed"}]),
        ("manifest_payload", {"changed": True}),
        ("provider_schema", {"changed": True}),
        ("chat_model", "deepseek-chat"),
        ("embedding_model", "changed-embedding"),
        ("retrieval_settings", {"query_variant_max_count": 2}),
        ("implementation_overrides", {"rag_v2.py": "version-b"}),
    ],
)
def test_every_semantic_change_alters_the_fingerprint(
    field: str,
    changed: object,
) -> None:
    if field == "retrieval_settings":
        changed = {
            **dict(_fingerprint()["retrieval_settings"]),  # type: ignore[arg-type]
            **dict(changed),  # type: ignore[arg-type]
        }
    assert _fingerprint(**{field: changed}) != _fingerprint()


def test_fingerprint_binds_answer_client_and_provider_configuration_code() -> None:
    paths = _fingerprint()["implementation_paths"]

    assert "backend/src/policy_api/answers/llm_client.py" in paths
    assert "backend/src/policy_api/config.py" in paths


def test_fingerprint_rejects_an_incomplete_retrieval_configuration() -> None:
    with pytest.raises(RagV2InputError, match="invalid_rag_v2_fingerprint"):
        _fingerprint(retrieval_settings={"query_variant_max_count": 3})


def test_resume_rejects_a_stale_fingerprint_instead_of_silently_reusing_it(
    tmp_path: Path,
) -> None:
    report = {
        "schema_version": 2,
        "configuration": {"mode": "real_model"},
        "fingerprint": _fingerprint(),
        "results": [{"case_id": "RAG-V2-001"}],
    }
    output = tmp_path / "partial.json"
    output.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(
        EvaluationFingerprintMismatch,
        match="evaluation_fingerprint_mismatch",
    ):
        load_rag_v2_resumable_results(
            output,
            configuration={"mode": "real_model"},
            fingerprint=_fingerprint(chat_model="changed-model"),
            allowed_case_ids={"RAG-V2-001"},
        )


def test_case_scorer_separates_clarification_recall_from_precision() -> None:
    cases = load_rag_v2_cases(DATASET)
    manifest = load_rag_v2_manifest(MANIFEST, cases=cases)
    case = next(item for item in cases if item["id"] == "RAG-V2-001")
    trace = RagV2Trace(
        actual_status="needs_clarification",
        answer_text="制度给出了住宿与餐补标准，但还不能计算。",
        clarification_questions=(
            "适用哪一城市档位？",
            "实际住宿几晚？",
            "是否有主办方提供全天餐食？",
        ),
        citation_sources=("差旅费用管理制度.md",),
        retrieved_top_5_sources=("差旅费用管理制度.md",),
        retrieved_disabled_sources=(),
        citation_validation_passed=True,
        retrieval_latency_ms=120,
        end_to_end_latency_ms=1200,
    )

    result = score_rag_v2_case(case, manifest[case["id"]], trace)

    assert result["specific_terminal_correctness_passed"] is True
    assert result["clarification_precision_passed"] is True
    assert result["clarification_recall_passed"] is True
    assert result["citation_support_passed"] is True
    assert result["unsupported_definitive_answer"] is False
    assert result["clarification_questions"] == list(trace.clarification_questions)
    assert result["citation_sources"] == ["差旅费用管理制度.md"]


def test_case_scorer_rejects_a_derived_total_for_a_formula_only_case() -> None:
    cases = load_rag_v2_cases(DATASET)
    manifest = load_rag_v2_manifest(MANIFEST, cases=cases)
    case = next(item for item in cases if item["id"] == "RAG-V2-003")
    trace = RagV2Trace(
        actual_status="answered",
        answer_text="住宿为2晚×350元/晚，餐补为3天×100元/天，总计1000元。",
        clarification_questions=(),
        citation_sources=("差旅费用管理制度.md",),
        retrieved_top_5_sources=("差旅费用管理制度.md",),
        retrieved_disabled_sources=(),
        citation_validation_passed=True,
        retrieval_latency_ms=100,
        end_to_end_latency_ms=1000,
    )

    result = score_rag_v2_case(case, manifest[case["id"]], trace)

    assert result["answer_correctness_passed"] is False
    assert result["unsupported_definitive_answer"] is True
    assert result["answer_text"] == trace.answer_text


def test_case_scorer_counts_disabled_retrieval_even_when_terminal_is_safe() -> None:
    cases = load_rag_v2_cases(DATASET)
    manifest = load_rag_v2_manifest(MANIFEST, cases=cases)
    case = next(item for item in cases if item["id"] == "RAG-V2-006")
    trace = RagV2Trace(
        actual_status="abstained",
        answer_text=None,
        clarification_questions=(),
        citation_sources=(),
        retrieved_top_5_sources=(),
        retrieved_disabled_sources=("火星出差管理办法（虚构演示）-录制A.txt",),
        citation_validation_passed=None,
        retrieval_latency_ms=90,
        end_to_end_latency_ms=900,
    )

    result = score_rag_v2_case(case, manifest[case["id"]], trace)

    assert result["correct_abstention_passed"] is True
    assert result["disabled_retrieval_violation"] is True
    assert result["no_evidence_conflict_must_abstain_passed"] is True


def test_report_redacts_nested_secrets_and_includes_gate_evidence() -> None:
    cases = load_rag_v2_cases(DATASET)
    manifest = load_rag_v2_manifest(MANIFEST, cases=cases)
    report = build_rag_v2_report(
        cases=cases,
        manifest=manifest,
        results=[],
        configuration={
            "mode": "real_model",
            "api_key": "hidden-value",
            "nested": {"authorization": "hidden-value", "safe": "kept"},
        },
        fingerprint=_fingerprint(),
        complete=False,
    )

    serialized = json.dumps(report, ensure_ascii=False)
    assert report["schema_version"] == 2
    assert report["status"] == "in_progress"
    assert report["coverage"]["catalog_total"] == 22
    assert report["configuration"] == {
        "mode": "real_model",
        "nested": {"safe": "kept"},
    }
    assert report["quality_gate"]["passed"] is False
    assert "hidden-value" not in serialized
