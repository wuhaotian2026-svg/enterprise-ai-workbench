from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
DATASET = ROOT / "sample-data" / "evaluation" / "cases.jsonl"
POLICIES = ROOT / "sample-data" / "policies"
REQUIRED_FIELDS = {"id", "question", "expected_status", "expected_facts", "expected_sources", "tags"}
REQUIRED_TAGS = {"direct", "paraphrase", "cross_section", "numeric", "false_premise", "conflict", "disabled_document"}


def load_cases() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_evaluation_dataset_has_balanced_contract_and_hidden_holdout() -> None:
    cases = load_cases()
    assert len(cases) >= 40
    assert all(REQUIRED_FIELDS <= case.keys() for case in cases)
    assert len({case["id"] for case in cases}) == len(cases)
    assert all(case["expected_status"] in {"answered", "abstained"} for case in cases)
    assert all(isinstance(case["expected_facts"], list) and isinstance(case["expected_sources"], list) and isinstance(case["tags"], list) for case in cases)
    counts = Counter(case["expected_status"] for case in cases)
    assert counts["abstained"] / len(cases) >= .25
    tags = {tag for case in cases for tag in case["tags"]}
    assert REQUIRED_TAGS <= tags
    assert sum("holdout" in case["tags"] for case in cases) >= 8


def test_policy_sources_are_fictional_versioned_and_referenced() -> None:
    files = sorted(POLICIES.glob("*.md"))
    assert len(files) == 5
    contents = {file.name: file.read_text(encoding="utf-8") for file in files}
    assert all("虚构" in text and "仅用于演示" in text for text in contents.values())
    assert all("示例科技" not in text and "张三" not in text and "李四" not in text for text in contents.values())
    cases = load_cases()
    referenced = {source for case in cases for source in case["expected_sources"]}
    assert referenced <= set(contents)
    assert "冲突测试" in contents["信息与版本说明.md"]
