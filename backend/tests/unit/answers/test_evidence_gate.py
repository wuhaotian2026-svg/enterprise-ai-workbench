from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from policy_api.answers.evidence import EvidenceSelector, evaluate_evidence
from policy_api.answers.schemas import EvidenceChunk
from policy_api.models import RefusalReason
from policy_api.retrieval.types import RetrievalResult


def chunk(text: str, score: float = 0.9, active: bool = True) -> EvidenceChunk:
    return EvidenceChunk(chunk_id=text, text=text, score=score, source_active=active)


def result(
    text: str,
    *,
    fused_score: float,
    lexical_score: float | None,
    vector_score: float | None,
) -> RetrievalResult:
    identifier = uuid.uuid5(uuid.NAMESPACE_DNS, text)
    return RetrievalResult(
        chunk_id=identifier,
        document_id=uuid.uuid5(uuid.NAMESPACE_URL, text),
        document_name="Policy.txt",
        text=text,
        page=1,
        heading_path="Policy",
        location="page:1",
        lexical_score=lexical_score,
        vector_score=vector_score,
        fused_score=fused_score,
        matched_by=tuple(
            name
            for name, score in (
                ("lexical", lexical_score),
                ("vector", vector_score),
            )
            if score is not None
        ),
    )


def test_gate_rejects_no_evidence_low_score_and_inactive_source() -> None:
    assert evaluate_evidence("如何请假", [], threshold=0.72).reason == RefusalReason.NO_EVIDENCE
    assert evaluate_evidence("如何请假", [chunk("请假需审批", 0.5)], threshold=0.72).reason == RefusalReason.LOW_CONFIDENCE
    decision = evaluate_evidence("如何请假", [chunk("请假需审批", active=False)], threshold=0.72)
    assert not decision.allowed and decision.reason == RefusalReason.CORPUS_UNAVAILABLE


@pytest.mark.parametrize(
    ("question", "evidence"),
    [
        ("住宿最多报销多少钱？", "住宿需要发票。"),
        ("年假有多少天？", "年假需要审批。"),
        ("费用报销比例是多少？", "费用需要主管审批。"),
    ],
)
def test_gate_rejects_missing_required_numeric_fact(question: str, evidence: str) -> None:
    decision = evaluate_evidence(question, [chunk(evidence)], threshold=0.72)
    assert not decision.allowed and decision.reason == RefusalReason.LOW_CONFIDENCE


def test_gate_rejects_conflicting_evidence_without_picking_a_side() -> None:
    evidence = [chunk("住宿上限500元", 0.95), chunk("住宿上限800元", 0.92)]
    decision = evaluate_evidence("住宿最多报销多少钱？", evidence, threshold=0.72)
    assert not decision.allowed and decision.reason == RefusalReason.CONFLICTING_EVIDENCE
    assert decision.selected_chunks == ()


def test_gate_allows_sufficient_consistent_active_evidence() -> None:
    evidence = [chunk("住宿费每晚最高500元，超过部分不予报销。", 0.91), chunk("住宿报销需提供发票。", 0.84)]
    decision = evaluate_evidence("住宿最多报销多少钱？", evidence, threshold=0.72)
    assert decision.allowed and decision.reason is None
    assert decision.score == pytest.approx(0.91)
    assert decision.selected_chunks == tuple(evidence)


def test_selector_uses_channel_relevance_not_fused_rank_score() -> None:
    candidate = result(
        "出差住宿标准为其他城市350元每晚，餐补100元每天。",
        fused_score=0.0149,
        vector_score=0.876,
        lexical_score=None,
    )
    decision = EvidenceSelector(
        semantic_threshold=0.85,
        lexical_threshold=0.30,
        dual_channel_minimum=0.20,
        max_chunks=6,
    ).select(
        "南京出差三天多少钱",
        [candidate],
        source_is_active=lambda _identifier: True,
    )

    assert decision.allowed
    assert decision.score == pytest.approx(0.876)
    assert decision.selected_chunks[0].chunk_id == str(candidate.chunk_id)


def test_selector_does_not_accept_two_low_channel_scores() -> None:
    candidate = result(
        "请假需要审批。",
        fused_score=0.2,
        vector_score=0.19,
        lexical_score=0.19,
    )
    decision = EvidenceSelector(0.85, 0.30, 0.20, 6).select(
        "如何请假", [candidate], source_is_active=lambda _identifier: True
    )

    assert not decision.allowed
    assert decision.reason == RefusalReason.LOW_CONFIDENCE


def test_selector_preserves_active_required_fact_and_conflict_gates() -> None:
    selector = EvidenceSelector(0.85, 0.30, 0.20, 6)
    sufficient = result(
        "住宿上限500元。",
        fused_score=0.01,
        vector_score=0.9,
        lexical_score=None,
    )
    inactive = selector.select(
        "住宿多少钱", [sufficient], source_is_active=lambda _identifier: False
    )
    assert inactive.reason == RefusalReason.CORPUS_UNAVAILABLE

    missing = result(
        "住宿需要发票。",
        fused_score=0.9,
        vector_score=0.9,
        lexical_score=None,
    )
    assert selector.select(
        "住宿多少钱", [missing], source_is_active=lambda _identifier: True
    ).reason == RefusalReason.LOW_CONFIDENCE

    conflicting = result(
        "住宿上限800元。",
        fused_score=0.009,
        vector_score=0.89,
        lexical_score=None,
    )
    assert selector.select(
        "住宿多少钱",
        [sufficient, conflicting],
        source_is_active=lambda _identifier: True,
    ).reason == RefusalReason.CONFLICTING_EVIDENCE


def test_selector_skips_markdown_heading_before_applying_chunk_limit() -> None:
    heading = result(
        "## 3. 住宿标准",
        fused_score=0.02,
        vector_score=0.95,
        lexical_score=0.60,
    )
    body = result(
        "其他城市住宿标准为每人每晚350元。",
        fused_score=0.01,
        vector_score=0.90,
        lexical_score=0.55,
    )

    decision = EvidenceSelector(0.85, 0.30, 0.20, 1).select(
        "住宿最多报销多少钱？",
        [heading, body],
        source_is_active=lambda _identifier: True,
    )

    assert decision.allowed
    assert len(decision.selected_chunks) == 1
    assert decision.selected_chunks[0].chunk_id == str(body.chunk_id)


def test_selector_concentrates_primary_source_before_filling_other_sources() -> None:
    primary_document_id = uuid.uuid4()
    primary_first = replace(
        result(
            "制度正文第一段。",
            fused_score=0.03,
            vector_score=0.95,
            lexical_score=0.60,
        ),
        document_id=primary_document_id,
    )
    secondary = result(
        "另一制度正文。",
        fused_score=0.02,
        vector_score=0.94,
        lexical_score=0.59,
    )
    primary_second = replace(
        result(
            "制度正文第二段。",
            fused_score=0.01,
            vector_score=0.93,
            lexical_score=0.58,
        ),
        document_id=primary_document_id,
    )

    decision = EvidenceSelector(0.85, 0.30, 0.20, 2).select(
        "如何办理？",
        [primary_first, secondary, primary_second],
        source_is_active=lambda _identifier: True,
    )

    assert decision.allowed
    assert [chunk.chunk_id for chunk in decision.selected_chunks] == [
        str(primary_first.chunk_id),
        str(primary_second.chunk_id),
    ]


def test_selector_preserves_competing_multi_chunk_source_inside_original_budget() -> None:
    primary_document_id = uuid.uuid4()
    competing_document_id = uuid.uuid4()
    primary_first = replace(
        result(
            "共享词面很多但主题不完整的第一段。",
            fused_score=0.05,
            vector_score=0.95,
            lexical_score=0.95,
        ),
        document_id=primary_document_id,
    )
    competing_context = replace(
        result(
            "目标制度的主题说明。",
            fused_score=0.04,
            vector_score=0.94,
            lexical_score=0.80,
        ),
        document_id=competing_document_id,
    )
    competing_rule = replace(
        result(
            "目标制度规定应在2个工作日内完成。",
            fused_score=0.03,
            vector_score=0.93,
            lexical_score=0.79,
        ),
        document_id=competing_document_id,
    )
    primary_second = replace(
        result(
            "共享词面的第二段。",
            fused_score=0.02,
            vector_score=0.92,
            lexical_score=0.78,
        ),
        document_id=primary_document_id,
    )
    primary_late = replace(
        result(
            "共享词面的第三段。",
            fused_score=0.01,
            vector_score=0.91,
            lexical_score=0.77,
        ),
        document_id=primary_document_id,
    )

    decision = EvidenceSelector(0.85, 0.30, 0.20, 4).select(
        "目标制度期限是什么？",
        [
            primary_first,
            competing_context,
            competing_rule,
            primary_second,
            primary_late,
        ],
        source_is_active=lambda _identifier: True,
    )

    assert decision.allowed
    selected_ids = {chunk.chunk_id for chunk in decision.selected_chunks}
    assert str(competing_context.chunk_id) in selected_ids
    assert str(competing_rule.chunk_id) in selected_ids


def test_selector_prefers_document_names_matching_the_question_topic() -> None:
    lexical_collision_document_id = uuid.uuid4()
    target_document_id = uuid.uuid4()
    lexical_collision = replace(
        result(
            "第3个工作日可补交申请。",
            fused_score=0.05,
            vector_score=0.95,
            lexical_score=1.0,
        ),
        document_id=lexical_collision_document_id,
        document_name="员工休假与考勤制度.md",
    )
    target_context = replace(
        result(
            "紧急事项须说明原因。",
            fused_score=0.04,
            vector_score=0.94,
            lexical_score=0.80,
        ),
        document_id=target_document_id,
        document_name="采购与审批规定.md",
    )
    target_rule = replace(
        result(
            "紧急采购应在2个工作日内补齐审批。",
            fused_score=0.03,
            vector_score=0.93,
            lexical_score=0.79,
        ),
        document_id=target_document_id,
        document_name="采购与审批规定.md",
    )

    decision = EvidenceSelector(0.85, 0.30, 0.20, 4).select(
        "紧急采购后补审批的期限是什么？",
        [lexical_collision, target_context, target_rule],
        source_is_active=lambda _identifier: True,
    )

    assert decision.allowed
    assert {chunk.chunk_id for chunk in decision.selected_chunks} == {
        str(target_context.chunk_id),
        str(target_rule.chunk_id),
    }


def test_selector_keeps_first_document_when_body_matches_more_than_one_title_cue() -> None:
    direct_document_id = uuid.uuid4()
    title_only_document_id = uuid.uuid4()
    direct_rule = replace(
        result(
            "拆分同一事项以规避审批额度的，多笔金额合并计算。",
            fused_score=0.05,
            vector_score=0.96,
            lexical_score=0.90,
        ),
        document_id=direct_document_id,
        document_name="费用报销管理办法.md",
    )
    title_only_rule = replace(
        result(
            "不得拆分订单规避审批或比价要求。",
            fused_score=0.04,
            vector_score=0.95,
            lexical_score=0.80,
        ),
        document_id=title_only_document_id,
        document_name="采购与审批规定.md",
    )

    decision = EvidenceSelector(0.85, 0.30, 0.20, 4).select(
        "同一事项拆成两张发票，审批额度可以分别计算吗？",
        [direct_rule, title_only_rule],
        source_is_active=lambda _identifier: True,
    )

    assert decision.allowed
    assert {chunk.chunk_id for chunk in decision.selected_chunks} == {
        str(direct_rule.chunk_id)
    }


def test_selector_uses_expense_business_object_before_generic_approval_title_match() -> None:
    procurement_document_id = uuid.uuid4()
    expense_document_id = uuid.uuid4()
    procurement_rule = replace(
        result(
            "不得拆分订单规避审批要求。",
            fused_score=0.06,
            vector_score=0.97,
            lexical_score=0.90,
        ),
        document_id=procurement_document_id,
        document_name="采购与审批规定.md",
    )
    expense_rule = replace(
        result(
            "同一事项的多张票据应合并计算报销审批额度。",
            fused_score=0.05,
            vector_score=0.95,
            lexical_score=0.88,
        ),
        document_id=expense_document_id,
        document_name="费用报销管理办法.md",
    )

    decision = EvidenceSelector(0.85, 0.30, 0.20, 4).select(
        "同一事项分成多张发票，审批额度能分别判断吗？",
        [procurement_rule, expense_rule],
        source_is_active=lambda _identifier: True,
    )

    assert decision.allowed
    assert {chunk.chunk_id for chunk in decision.selected_chunks} == {
        str(expense_rule.chunk_id)
    }


def test_selector_promotes_direct_negative_rule_above_unrelated_high_ranked_context() -> None:
    unrelated = [
        replace(
            result(
                f"无关流程说明第{index}项。",
                fused_score=0.06 - index * 0.001,
                vector_score=0.96,
                lexical_score=0.35,
            ),
            document_name="通用流程说明.md",
        )
        for index in range(6)
    ]
    direct_negative = replace(
        result(
            "制度未规定固定远程办公额度，具体安排须另行书面批准。",
            fused_score=0.04,
            vector_score=0.90,
            lexical_score=0.34,
        ),
        document_name="员工安排制度.md",
    )

    decision = EvidenceSelector(0.85, 0.30, 0.20, 6).select(
        "公司是不是固定有远程办公额度？",
        [*unrelated, direct_negative],
        source_is_active=lambda _identifier: True,
    )

    assert decision.allowed
    assert str(direct_negative.chunk_id) in {
        chunk.chunk_id for chunk in decision.selected_chunks
    }
