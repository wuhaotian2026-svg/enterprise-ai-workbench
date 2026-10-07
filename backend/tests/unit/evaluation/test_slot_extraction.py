from __future__ import annotations

import json
from pathlib import Path

from policy_api.evaluation.slot_extraction import (
    PUBLIC_CATEGORY_DISTRIBUTION,
    SlotExtractionTrace,
    assess_slot_extraction_quality,
    load_slot_extraction_catalog,
    score_slot_extraction_results,
)


BACKEND_ROOT = Path(__file__).resolve().parents[3]
CASES_PATH = BACKEND_ROOT / "evaluation" / "slot_extraction_cases.json"
MANIFEST_PATH = BACKEND_ROOT / "evaluation" / "slot_extraction_manifest.json"


def test_manifest_loader_normalizes_hr_year_to_canonical_integer(
    tmp_path: Path,
) -> None:
    cases_path = tmp_path / "cases.json"
    manifest_path = tmp_path / "manifest.json"
    cases_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": [
                    {
                        "id": "hr-year-followup",
                        "module": "hr",
                        "category": "followup",
                        "current_user_turn": "年份2027",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": [
                    {
                        "case_id": "hr-year-followup",
                        "module": "hr",
                        "category": "followup",
                        "applicable_metrics": ["field_precision", "field_recall"],
                        "expected_fields": {"year": "2027"},
                        "expected_pending": {},
                        "expected_rejected": [],
                        "expected_clarification_fields": [],
                        "expected_terminal": "accepted",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    _cases, manifest = load_slot_extraction_catalog(cases_path, manifest_path)

    assert manifest["hr-year-followup"].expected_fields["year"] == 2027
    assert isinstance(manifest["hr-year-followup"].expected_fields["year"], int)


def test_public_dataset_has_exact_distribution_and_required_real_phrases() -> None:
    cases, manifest = load_slot_extraction_catalog(CASES_PATH, MANIFEST_PATH)

    assert len(cases) == 60
    assert len(manifest) == 60
    for module in ("hr", "procurement"):
        distribution = {
            category: sum(
                case.module == module and case.category == category
                for case in cases
            )
            for category in PUBLIC_CATEGORY_DISTRIBUTION
        }
        assert distribution == PUBLIC_CATEGORY_DISTRIBUTION

    current_turns = {case.current_user_turn for case in cases}
    assert {
        "我想申请9.1-9.6年假，用于探亲",
        "就是2026年，用于探亲",
        "年份是2027年",
        "补休 2026年11月9号至2026年11月10号 原因陪同就医",
        "申请标题是办公椅 用途放在办公室 9.30需要 人民币",
        "需要日期为2026.9.30",
        "标题为办公用品，买一个桌子，单价600",
    } <= current_turns

    case_by_id = {case.id: case for case in cases}
    partial = manifest["SE-PROC-019"]
    assert partial.expected_fields == {"title": "办公用品"}
    assert partial.expected_pending == {"items": "item_fields_required"}
    assert partial.expected_clarification_fields == [
        "purpose",
        "needed_by_date",
        "currency",
        "items[0].unit",
        "items[0].category_code",
    ]
    assert manifest["SE-PROC-010"].expected_clarification_fields == [
        "items[1].unit",
        "items[1].estimated_unit_price",
    ]
    assert manifest["SE-PROC-016"].expected_clarification_fields == [
        "needed_by_date",
    ]
    assert manifest["SE-PROC-017"].expected_clarification_fields == [
        "needed_by_year",
    ]

    category_phrases = ("办公用品类", "IT设备类", "软件服务类", "专业服务类")
    for case_id, expected in manifest.items():
        if case_by_id[case_id].module != "procurement":
            continue
        if "items" not in expected.expected_fields:
            continue
        assert any(
            phrase in case_by_id[case_id].current_user_turn
            for phrase in category_phrases
        ), case_id


def test_scorer_uses_explicit_applicable_denominators() -> None:
    cases, manifest = load_slot_extraction_catalog(CASES_PATH, MANIFEST_PATH)
    selected = [cases[0], cases[1]]
    selected_manifest = {case.id: manifest[case.id] for case in selected}
    traces = {
        case.id: SlotExtractionTrace.fixture_from_expectation(selected_manifest[case.id])
        for case in selected
    }

    report = score_slot_extraction_results(selected, selected_manifest, traces)

    expected_precision = sum(len(trace.accepted_fields) for trace in traces.values())
    expected_recall = sum(
        len(selected_manifest[case.id].expected_fields)
        for case in selected
        if "field_recall" in selected_manifest[case.id].applicable_metrics
    )
    assert report["metrics"]["field_precision"]["denominator"] == expected_precision
    assert report["metrics"]["field_recall"]["denominator"] == expected_recall


def test_pending_and_rejected_values_never_count_as_precision_predictions() -> None:
    cases, manifest = load_slot_extraction_catalog(CASES_PATH, MANIFEST_PATH)
    case = next(item for item in cases if manifest[item.id].expected_pending)
    expected = manifest[case.id]
    trace = SlotExtractionTrace.fixture_from_expectation(expected)

    report = score_slot_extraction_results(
        [case], {case.id: expected}, {case.id: trace}
    )

    assert report["metrics"]["field_precision"]["denominator"] == len(
        trace.accepted_fields
    )
    assert set(trace.pending) == set(expected.expected_pending)


def test_report_contains_no_user_text_source_quote_or_business_values() -> None:
    cases, manifest = load_slot_extraction_catalog(CASES_PATH, MANIFEST_PATH)
    case = cases[0]
    trace = SlotExtractionTrace.fixture_from_expectation(manifest[case.id])

    report = score_slot_extraction_results(
        [case], {case.id: manifest[case.id]}, {case.id: trace}
    )
    serialized = str(report)

    assert case.current_user_turn not in serialized
    for value in manifest[case.id].expected_fields.values():
        assert str(value) not in serialized


def test_quality_gate_freezes_thresholds_and_zero_tolerance_safety() -> None:
    perfect = {
        "metrics": {
            "envelope_valid": {"value": 1.0},
            "field_precision": {"value": 1.0},
            "field_recall": {"value": 1.0},
            "ambiguity_handled": {"value": 1.0},
            "source_verified": {"value": 1.0},
            "clarification_correct": {"value": 1.0},
            "terminal_correct": {"value": 0.95},
            "must_not_execute": {"value": 1.0},
            "call_budget_respected": {"value": 1.0},
            "extraction_latency_p95_ms": 3000,
            "incomplete_turn_latency_p95_ms": 4000,
            "total_turn_latency_p95_ms": 8000,
            "write_executed": 0,
            "created_resources": 0,
            "duplicate_resources": 0,
        }
    }

    assert assess_slot_extraction_quality(perfect)["status"] == "passed"
    perfect["metrics"]["write_executed"] = 1
    assert assess_slot_extraction_quality(perfect)["status"] == "failed"
