from __future__ import annotations

import pytest

from policy_api.retrieval.query_variants import QueryVariantBuilder
from policy_api.retrieval.service import RetrievalError


def test_specific_query_variants_keep_entities_dates_and_numbers() -> None:
    variants = QueryVariantBuilder(max_count=3).build(
        "  请问我九月份去南京出差三天，差旅费是多少？  "
    )

    assert variants[0] == "请问我九月份去南京出差三天,差旅费是多少?"
    assert 1 <= len(variants) <= 3
    assert len(variants) == len(set(variants))
    assert all("南京" in item and "九月份" in item and "三天" in item for item in variants)
    assert all("一线城市" not in item and "其他城市" not in item for item in variants)
    assert any("标准" in item and "上限" in item and "报销" in item for item in variants)


def test_variants_never_add_missing_business_fields() -> None:
    variants = QueryVariantBuilder(max_count=3).build("年假怎么申请")
    joined = "\n".join(variants)

    assert "2026" not in joined
    assert "5天" not in joined
    assert "employee_id" not in joined


def test_variants_are_deduplicated_and_bounded() -> None:
    assert QueryVariantBuilder(max_count=3).build("费用标准上限") == (
        "费用标准上限",
        "费用标准上限 补助 报销",
    )
    assert QueryVariantBuilder(max_count=1).build("请问报销多少钱？") == (
        "请问报销多少钱?",
    )
    with pytest.raises(ValueError, match="query_variant_max_count_invalid"):
        QueryVariantBuilder(max_count=4)


def test_empty_query_is_rejected_before_retrieval() -> None:
    with pytest.raises(RetrievalError, match="empty_question"):
        QueryVariantBuilder().build("  \u3000  ")


def test_fixed_entitlement_question_adds_negative_policy_retrieval_terms() -> None:
    variants = QueryVariantBuilder().build("公司是不是每周固定提供远程办公额度？")

    assert variants[0] == "公司是不是每周固定提供远程办公额度?"
    assert any(
        "未规定" in variant and "固定额度" in variant and "批准" in variant
        for variant in variants[1:]
    )
    assert all("每周" in variant and "远程办公" in variant for variant in variants)
