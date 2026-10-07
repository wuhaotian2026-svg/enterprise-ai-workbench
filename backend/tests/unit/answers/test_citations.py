from __future__ import annotations

from policy_api.answers.citations import validate_citations
from policy_api.answers.schemas import EvidenceChunk


def chunk(identifier: str, text: str, active: bool = True) -> EvidenceChunk:
    return EvidenceChunk(identifier, text, 0.9, active)


def test_citations_must_exist_and_remain_active() -> None:
    evidence = [chunk("c1", "住宿上限500元")]
    assert validate_citations("上限500元。", ("outside",), evidence).valid is False
    assert validate_citations("上限500元。", ("c1",), [chunk("c1", "住宿上限500元", False)]).valid is False


def test_citations_must_support_every_numeric_fact_in_answer() -> None:
    evidence = [chunk("c1", "住宿上限500元，自2026年8月1日起执行，报销比例为80%。")]
    assert validate_citations("上限500元，2026年8月1日起按80%报销。", ("c1",), evidence).valid
    decision = validate_citations("上限800元，2026年8月1日起按80%报销。", ("c1",), evidence)
    assert not decision.valid and decision.reason == "unsupported_numeric_fact"


def test_multiple_citations_may_jointly_support_answer() -> None:
    evidence = [chunk("c1", "年假为5天。"), chunk("c2", "申请需提前3个工作日。")]
    assert validate_citations("年假5天，需提前3个工作日申请。", ("c1", "c2"), evidence).valid


def test_numeric_value_repeated_from_question_is_not_treated_as_a_new_claim() -> None:
    evidence = [chunk("c1", "2,000元（含）至5,000元，由部门负责人审批。")]
    assert validate_citations(
        "3000元报销由部门负责人审批。", ("c1",), evidence,
        question="一笔3000元报销要找哪一级审批？",
    ).valid
    assert not validate_citations(
        "3000元报销上限为8000元。", ("c1",), evidence,
        question="一笔3000元报销要找哪一级审批？",
    ).valid


def test_thousands_separators_do_not_change_numeric_identity() -> None:
    evidence = [chunk("c1", "超过5000元由分管负责人审批。")]
    assert validate_citations(
        "7,000元由分管负责人审批。", ("c1",), evidence,
        question="预计花费7000元要经过哪些审批？",
    ).valid


def test_formula_uses_only_question_or_citation_numbers() -> None:
    evidence = [chunk("c1", "其他城市住宿标准为350元每晚。")]
    assert validate_citations(
        "按2晚 × 350元/晚计算。",
        ("c1",),
        evidence,
        question="住宿2晚如何计算？",
    ).valid
    decision = validate_citations(
        "按2晚 × 350元/晚计算，共1000元。",
        ("c1",),
        evidence,
        question="住宿2晚如何计算？",
    )
    assert not decision.valid
    assert decision.reason == "unsupported_numeric_fact"


def test_correct_formula_result_may_be_derived_from_supported_operands() -> None:
    evidence = [chunk("c1", "其他城市住宿标准为350元每晚。")]

    decision = validate_citations(
        "住宿费计算式：350元/晚 × 2晚 = 700元。",
        ("c1",),
        evidence,
        question="已确认适用其他城市标准，住宿2晚，请列住宿费计算式。",
    )

    assert decision.valid
    assert decision.reason is None


def test_correct_per_person_rate_formula_is_a_supported_derivation() -> None:
    evidence = [
        chunk("c1", "住宿费标准为每人每晚420元。"),
        chunk("c2", "餐费标准为每人每天85元。"),
    ]

    decision = validate_citations(
        "住宿费计算式：4晚 × 420元/人/晚 = 1680元/人。",
        ("c1",),
        evidence,
        question="住宿4晚，请列住宿费计算式。",
    )

    assert decision.valid
    assert decision.reason is None

    meal_decision = validate_citations(
        "餐费计算式：3天 × 85元/人/天 = 255元/人。",
        ("c2",),
        evidence,
        question="用餐3天，请列餐费计算式。",
    )

    assert meal_decision.valid
    assert meal_decision.reason is None


def test_correct_result_before_parenthesized_formula_is_supported() -> None:
    evidence = [chunk("c1", "设备补贴标准为每台每月240元。")]

    decision = validate_citations(
        "三个月的补贴上限为720元（3月 × 240元/月）。",
        ("c1",),
        evidence,
        question="设备使用3个月，请列补贴计算依据。",
    )

    assert decision.valid
    assert decision.reason is None


def test_chinese_quantity_in_question_supports_canonical_formula_operand() -> None:
    evidence = [chunk("c1", "餐费标准为每人每天90元。")]

    decision = validate_citations(
        "餐费计算式：3天 × 90元/人/天 = 270元。",
        ("c1",),
        evidence,
        question="出差三天且未提供全天餐食，请列餐费计算式。",
    )

    assert decision.valid
    assert decision.reason is None


def test_chinese_quantity_support_does_not_trust_unmentioned_numbers() -> None:
    evidence = [chunk("c1", "住宿标准为每人每晚430元。")]

    decision = validate_citations(
        "住宿费计算式：4晚 × 430元/人/晚 = 1720元。",
        ("c1",),
        evidence,
        question="住宿两晚，请列住宿费计算式。",
    )

    assert not decision.valid
    assert decision.reason == "unsupported_numeric_fact"
