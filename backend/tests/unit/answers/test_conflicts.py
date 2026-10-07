from __future__ import annotations

from policy_api.answers.conflicts import detect_conflicts
from policy_api.answers.schemas import EvidenceChunk


def chunk(text: str) -> EvidenceChunk:
    return EvidenceChunk(chunk_id=text, text=text, score=0.9, source_active=True)


def test_detects_conflicting_money_days_and_percentages_for_same_fact() -> None:
    assert detect_conflicts([chunk("Lodging limit 500 \u5143"), chunk("Lodging limit 800 \u5143")]).conflicting
    assert detect_conflicts([chunk("Annual leave is 5 \u5929"), chunk("Annual leave is 10 \u5929")]).conflicting
    assert detect_conflicts([chunk("Reimbursement ratio 50%"), chunk("Reimbursement ratio 80%")]).conflicting


def test_unrelated_numeric_facts_are_not_a_conflict() -> None:
    decision = detect_conflicts([
        chunk("Annual leave request requires 5 \u4e2a\u5de5\u4f5c\u65e5 notice."),
        chunk("Sick leave proof is due within 2 \u4e2a\u5de5\u4f5c\u65e5."),
    ])
    assert not decision.conflicting

    different_leave_types = detect_conflicts([
        chunk("\u4e8b\u5047\u4e3a\u65e0\u85aa\u5047\uff0c\u8fde\u7eed\u8d85\u8fc7 2 \u4e2a\u5de5\u4f5c\u65e5\u9700\u8981\u6279\u51c6\u3002"),
        chunk("\u75c5\u5047\u8fde\u7eed\u8d85\u8fc7 1 \u4e2a\u5de5\u4f5c\u65e5\u9700\u8981\u8bc1\u660e\u3002"),
    ])
    assert not different_leave_types.conflicting


def test_detects_opposite_permission_language() -> None:
    decision = detect_conflicts([chunk("Employees \u53ef\u4ee5 submit later"), chunk("Employees \u4e0d\u5f97 submit later")])
    assert decision.conflicting
    assert "permission" in decision.categories


def test_repeated_same_fact_is_not_a_conflict() -> None:
    decision = detect_conflicts([chunk("Lodging limit 500 \u5143"), chunk("Lodging limit remains 500 \u5143")])
    assert not decision.conflicting
