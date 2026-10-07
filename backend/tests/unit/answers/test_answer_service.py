from __future__ import annotations

import uuid

from policy_api.answers.llm_client import ModelAnswer
from policy_api.answers.service import AnswerService
from policy_api.models import RefusalReason
from policy_api.retrieval.types import RetrievalResult


def result(identifier: str, text: str, score: float = 0.9) -> RetrievalResult:
    return RetrievalResult(chunk_id=uuid.UUID(identifier), document_id=uuid.uuid4(), document_name="Policy.txt",
        text=text, page=1, heading_path="Policy", location="page:1",
        lexical_score=None, vector_score=score,
        fused_score=score, matched_by=("keyword", "vector"))


def test_gate_refusal_does_not_call_model() -> None:
    called = False
    def generate(*_args, **_kwargs):
        nonlocal called; called = True; raise AssertionError
    answer = AnswerService(retrieve=lambda _q: [], source_is_active=lambda _id: True,
        generate=generate, threshold=0.72).answer("公司有育儿补贴吗？")
    assert answer.status == "abstained" and answer.refusal_reason == RefusalReason.NO_EVIDENCE
    assert not called


def test_answer_service_returns_verified_answer_and_citations() -> None:
    evidence = [result("00000000-0000-0000-0000-000000000001", "住宿上限500元。")]
    generated_messages = []
    def generate(messages, allowed_citation_ids):
        generated_messages.extend(messages); assert allowed_citation_ids == {str(evidence[0].chunk_id)}
        return ModelAnswer("answered", "住宿上限500元。", (str(evidence[0].chunk_id),))
    answer = AnswerService(retrieve=lambda _q: evidence, source_is_active=lambda _id: True,
        generate=generate, threshold=0.72).answer("住宿最多报销多少钱？")
    assert answer.status == "answered" and answer.text == "住宿上限500元。"
    assert answer.citations == (str(evidence[0].chunk_id),) and generated_messages


def test_model_abstention_and_invalid_citation_never_return_unverified_text() -> None:
    evidence = [result("00000000-0000-0000-0000-000000000001", "住宿上限500元。")]
    abstained = AnswerService(retrieve=lambda _q:evidence, source_is_active=lambda _id:True,
        generate=lambda *_args, **_kwargs: ModelAnswer("abstained", None, ()), threshold=0.72).answer("住宿多少钱？")
    assert abstained.status == "abstained" and abstained.text is None
    invalid = AnswerService(retrieve=lambda _q:evidence, source_is_active=lambda _id:True,
        generate=lambda *_args, **_kwargs: ModelAnswer("answered", "住宿上限800元。", (str(evidence[0].chunk_id),)),
        threshold=0.72).answer("住宿多少钱？")
    assert invalid.status == "abstained" and invalid.text is None
    assert invalid.refusal_reason == RefusalReason.CITATION_VALIDATION_FAILED


def test_source_is_revalidated_after_retrieval() -> None:
    evidence = [result("00000000-0000-0000-0000-000000000001", "住宿上限500元。")]
    answer = AnswerService(retrieve=lambda _q:evidence, source_is_active=lambda _id:False,
        generate=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()), threshold=0.72).answer("住宿多少钱？")
    assert answer.refusal_reason == RefusalReason.CORPUS_UNAVAILABLE


def test_answered_shape_with_explicit_abstention_language_is_forced_to_abstain() -> None:
    evidence = [result("00000000-0000-0000-0000-000000000001", "制度未规定固定居家办公天数。")]
    answer = AnswerService(
        retrieve=lambda _q: evidence, source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "answered", "制度未规定固定天数，建议咨询负责人。", (str(evidence[0].chunk_id),)
        ), threshold=0.72,
    ).answer("每周固定允许几天居家办公？")
    assert answer.status == "abstained" and answer.text is None
    assert answer.refusal_reason == RefusalReason.LOW_CONFIDENCE


def test_citation_failure_is_repaired_once_with_the_same_allowed_ids() -> None:
    evidence = [result("00000000-0000-0000-0000-000000000001", "住宿上限500元。")]
    calls: list[tuple[list[dict[str, str]], set[str]]] = []

    def generate(messages, allowed_citation_ids):
        calls.append((messages, allowed_citation_ids))
        if len(calls) == 1:
            return ModelAnswer("answered", "住宿上限800元。", (str(evidence[0].chunk_id),))
        return ModelAnswer("answered", "住宿上限500元。", (str(evidence[0].chunk_id),))

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("住宿最多报销多少钱？")

    allowed = {str(evidence[0].chunk_id)}
    assert answer.status == "answered"
    assert answer.text == "住宿上限500元。"
    assert answer.citations == (str(evidence[0].chunk_id),)
    assert len(calls) == 2
    assert calls[0][1] == calls[1][1] == allowed
    assert "repairing one enterprise-policy answer" in calls[1][0][0]["content"]


def test_second_citation_failure_strictly_abstains_without_a_third_call() -> None:
    evidence = [result("00000000-0000-0000-0000-000000000001", "住宿上限500元。")]
    call_count = 0

    def generate(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        return ModelAnswer("answered", "住宿上限800元。", (str(evidence[0].chunk_id),))

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("住宿最多报销多少钱？")

    assert answer.status == "abstained"
    assert answer.refusal_reason == RefusalReason.CITATION_VALIDATION_FAILED
    assert call_count == 2


def test_model_abstention_never_starts_citation_repair() -> None:
    evidence = [result("00000000-0000-0000-0000-000000000001", "住宿上限500元。")]
    call_count = 0

    def generate(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        return ModelAnswer("abstained", None, ())

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("住宿最多报销多少钱？")

    assert answer.status == "abstained"
    assert answer.refusal_reason == RefusalReason.LOW_CONFIDENCE
    assert call_count == 1


def test_clarification_keeps_supported_text_citations_and_questions() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "其他城市住宿标准为350元每晚。",
    )]
    citation_id = str(evidence[0].chunk_id)
    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "needs_clarification",
            "其他城市住宿标准为350元每晚。",
            (citation_id,),
            ("适用哪一城市档位？", "实际住宿几晚？"),
        ),
        threshold=0.72,
    ).answer("南京出差三天多少钱？")

    assert answer.status == "needs_clarification"
    assert answer.text == "其他城市住宿标准为350元每晚。"
    assert answer.refusal_reason is None
    assert answer.citations == (citation_id,)
    assert answer.clarification_questions == (
        "适用哪一城市档位？",
        "实际住宿几晚？",
    )


def test_explicit_negative_policy_rule_is_answered() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "制度未规定固定居家办公天数，需另行书面批准。",
    )]
    citation_id = str(evidence[0].chunk_id)
    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "answered",
            "制度未规定固定居家办公天数，需另行书面批准。",
            (citation_id,),
        ),
        threshold=0.72,
    ).answer("公司每周固定允许居家办公2天吗？")

    assert answer.status == "answered"
    assert answer.text is not None


def test_clarification_citation_failure_is_repaired_once() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "其他城市住宿标准为350元每晚。",
    )]
    citation_id = str(evidence[0].chunk_id)
    calls: list[list[dict[str, str]]] = []

    def generate(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return ModelAnswer(
                "needs_clarification",
                "其他城市住宿标准为800元每晚。",
                (citation_id,),
                ("实际住宿几晚？",),
            )
        return ModelAnswer(
            "needs_clarification",
            "其他城市住宿标准为350元每晚。",
            (citation_id,),
            ("实际住宿几晚？",),
        )

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("南京出差多少钱？")

    assert answer.status == "needs_clarification"
    assert answer.clarification_questions == ("实际住宿几晚？",)
    assert len(calls) == 2
    assert '"status": "needs_clarification"' in calls[1][1]["content"]


def test_supported_clarification_language_is_not_forced_to_abstain() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "制度按甲类地点和乙类地点规定不同标准，适用类别以公司清单为准。",
    )]
    citation_id = str(evidence[0].chunk_id)

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "needs_clarification",
            "制度提供了两类标准，但无法确认当前地点适用哪个类别。",
            (citation_id,),
            ("该地点在公司清单中属于哪一类别？",),
        ),
        threshold=0.72,
    ).answer("这个地点适用什么标准？")

    assert answer.status == "needs_clarification"
    assert answer.refusal_reason is None
    assert answer.clarification_questions == (
        "该地点在公司清单中属于哪一类别？",
    )


def test_conditional_evidence_reconsiders_one_model_abstention_once() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "甲类地点上限500元，乙类地点上限350元；若提供全天服务，当日不发补助。",
    )]
    citation_id = str(evidence[0].chunk_id)
    calls: list[list[dict[str, str]]] = []

    def generate(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return ModelAnswer("abstained", None, ())
        return ModelAnswer(
            "needs_clarification",
            "制度提供了分类上限和全天服务例外。",
            (citation_id,),
            ("适用甲类还是乙类？", "需要计算几天？", "全天服务有几天？"),
        )

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("请计算这次活动补助。")

    assert answer.status == "needs_clarification"
    assert len(calls) == 2
    assert "reconsidering" in calls[1][0]["content"]


def test_specific_fee_question_reconsiders_conditional_abstention() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "甲类地点上限500元，乙类地点上限350元；若提供全天服务，当日不发补助。",
    )]
    citation_id = str(evidence[0].chunk_id)
    call_count = 0

    def generate(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelAnswer("abstained", None, ())
        return ModelAnswer(
            "needs_clarification",
            "费用取决于地点类别及是否提供全天服务。",
            (citation_id,),
            ("该地点属于甲类还是乙类？",),
        )

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("这次差旅费是多少？")

    assert answer.status == "needs_clarification"
    assert call_count == 2


def test_complete_deadline_question_does_not_use_conditional_reconsideration() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "若属于紧急事项，应在2个工作日内补齐审批，否则不予受理。",
    )]
    call_count = 0

    def generate(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        return ModelAnswer("abstained", None, ())

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("第3个工作日补审批是否已经超过期限？")

    assert answer.status == "abstained"
    assert call_count == 1


def test_unrelated_conditional_words_do_not_reconsider_a_fee_abstention() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "费用申请由其他部门适用不同审批流程；另一事项的费用标准为100元。",
    )]
    call_count = 0

    def generate(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        return ModelAnswer("abstained", None, ())

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("费用申请由其他部门适用不同审批流程，这次费用是多少？")

    assert answer.status == "abstained"
    assert call_count == 1


def test_negative_rule_uses_relevant_evidence_sentence_without_user_proposal_echo() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "交通中断须提交证明。制度未规定按月固定额度，具体安排须另行书面批准。",
    )]
    citation_id = str(evidence[0].chunk_id)

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "answered",
            "制度未规定每月固定3天的额度，具体安排须另行书面批准。",
            (citation_id,),
        ),
        threshold=0.72,
    ).answer("制度是否允许每月固定3天？")

    assert answer.status == "answered"
    assert answer.text == "制度未规定按月固定额度，具体安排须另行书面批准。"
    assert "3天" not in (answer.text or "")


def test_negative_rule_keeps_adjacent_required_action_from_evidence() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "制度未规定固定居家办公天数。具体安排须主管书面批准。",
    )]
    citation_id = str(evidence[0].chunk_id)

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "answered",
            "制度未规定每周固定2天；具体安排须主管书面批准。",
            (citation_id,),
        ),
        threshold=0.72,
    ).answer("制度是否允许每周固定2天居家办公？")

    assert answer.status == "answered"
    assert answer.text == "制度未规定固定居家办公天数。具体安排须主管书面批准。"
    assert "2天" not in (answer.text or "")


def test_plain_negative_phrase_falls_back_to_cited_combination_rule() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "拆分同一事项以规避额度的，多笔金额合并计算。",
    )]
    citation_id = str(evidence[0].chunk_id)

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "answered",
            "不可以。两笔640元合计1280元，应按合并金额判断。",
            (citation_id,),
        ),
        threshold=0.72,
    ).answer("同一事项拆成两笔640元，可以分别判断额度吗？")

    assert answer.status == "answered"
    assert answer.text == "拆分同一事项以规避额度的，多笔金额合并计算。"
    assert "1280元" not in (answer.text or "")


def test_direct_permission_denial_does_not_echo_positive_proposition() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "超过适用条件应事前申请，不得事后补办。",
    )]
    citation_id = str(evidence[0].chunk_id)

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "answered",
            "不可以直接事后登记；超过适用条件应事前申请，不得事后补办。",
            (citation_id,),
        ),
        threshold=0.72,
    ).answer("事项已经执行了，现在可以直接事后登记吗？")

    assert answer.status == "answered"
    assert answer.text == "超过适用条件应事前申请，不得事后补办。"
    assert "可以直接事后登记" not in (answer.text or "")


def test_plain_whether_question_keeps_complete_supported_compliance_answer() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "采购金额超过50000元由采购评审小组批准；超过20000元须签订书面合同；"
        "申请人以外的一名员工完成验收记录；不得拆分订单规避审批额度。",
    )]
    citation_id = str(evidence[0].chunk_id)
    model_text = (
        "采购60000元设备需要采购评审小组批准，须签订书面合同，"
        "并由申请人以外的一名员工完成验收记录；不得拆分订单规避审批额度。"
    )

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "answered",
            model_text,
            (citation_id,),
        ),
        threshold=0.72,
    ).answer("采购60000元设备需要哪一级批准、是否签合同、由谁验收？")

    assert answer.status == "answered"
    assert answer.text == model_text
    assert "采购评审小组批准" in (answer.text or "")
    assert "书面合同" in (answer.text or "")
    assert "完成验收记录" in (answer.text or "")


def test_answered_text_requesting_missing_user_fact_is_repaired_once() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "累计任职不满十年的，年休假为5个工作日；任职年限按入职日期确定。",
    )]
    citation_id = str(evidence[0].chunk_id)
    calls: list[list[dict[str, str]]] = []

    def generate(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return ModelAnswer(
                "answered",
                "具体天数需根据您的入职日期确定，请提供入职日期。",
                (citation_id,),
            )
        return ModelAnswer(
            "needs_clarification",
            "任职年限按入职日期确定。",
            (citation_id,),
            ("入职日期是哪一天？",),
        )

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("我尚不满一年，可以休多少天年假？")

    assert answer.status == "needs_clarification"
    assert answer.clarification_questions == ("入职日期是哪一天？",)
    assert len(calls) == 2
    assert "answered_contains_unresolved_user_fact" in calls[1][1]["content"]


def test_repaired_answer_still_requesting_user_fact_fails_closed_without_third_call() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "补助资格按参加工作日期确定。",
    )]
    citation_id = str(evidence[0].chunk_id)
    call_count = 0

    def generate(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        return ModelAnswer(
            "answered",
            "需要根据您的参加工作日期判断，请提供该日期。",
            (citation_id,),
        )

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("我能领取多少补助？")

    assert answer.status == "abstained"
    assert answer.text is None
    assert call_count == 2


def test_personal_calculation_that_only_restates_a_conditional_formula_is_repaired() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "新成员当期津贴按加入日期后的剩余自然日比例折算，标准额度为1000元。",
    )]
    citation_id = str(evidence[0].chunk_id)
    calls: list[list[dict[str, str]]] = []

    def generate(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return ModelAnswer(
                "answered",
                "新成员没有固定的当期津贴金额，按加入日期后的剩余自然日比例折算。",
                (citation_id,),
            )
        return ModelAnswer(
            "needs_clarification",
            "当期津贴按加入日期后的剩余自然日比例折算。",
            (citation_id,),
            ("您的加入日期是哪一天？",),
        )

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("我刚加入团队，本期能领多少津贴？")

    assert answer.status == "needs_clarification"
    assert answer.clarification_questions == ("您的加入日期是哪一天？",)
    assert len(calls) == 2
    assert "answered_contains_unresolved_conditional_calculation" in calls[1][1]["content"]


def test_clarification_repeating_a_known_lodging_night_count_is_repaired() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "甲类地点住宿上限为每晚420元，乙类地点为每晚310元。",
    )]
    citation_id = str(evidence[0].chunk_id)
    calls: list[list[dict[str, str]]] = []

    def generate(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return ModelAnswer(
                "needs_clarification",
                "住宿上限取决于适用地点类别和住宿晚数。",
                (citation_id,),
                ("适用哪个地点类别？", "住宿几晚？"),
            )
        return ModelAnswer("abstained", None, ())

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("本次驻场已经住宿四晚，按制度能报多少？")

    assert answer.status == "abstained"
    assert answer.refusal_reason == RefusalReason.CITATION_VALIDATION_FAILED
    assert len(calls) == 2
    assert "clarification_repeats_known_user_fact" in calls[1][1]["content"]


def test_explicit_negative_evidence_reconsiders_initial_abstention() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "制度未规定按周固定远程办公额度，具体安排须另行书面批准。",
    )]
    citation_id = str(evidence[0].chunk_id)
    call_count = 0

    def generate(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelAnswer("abstained", None, ())
        return ModelAnswer(
            "answered",
            "制度未规定按周固定远程办公额度，具体安排须另行书面批准。",
            (citation_id,),
        )

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=generate,
        threshold=0.72,
    ).answer("公司是不是每周固定有远程办公额度？")

    assert answer.status == "answered"
    assert call_count == 2


def test_supported_negative_entitlement_uses_cited_wording_instead_of_paraphrase() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "制度未规定固定远程办公额度，具体安排须另行书面批准。",
    )]
    citation_id = str(evidence[0].chunk_id)

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "answered",
            "公司制度未规定固定的远程办公额度，具体安排需要书面批准。",
            (citation_id,),
        ),
        threshold=0.72,
    ).answer("公司是不是每月固定有远程办公额度？")

    assert answer.status == "answered"
    assert answer.text == "制度未规定固定远程办公额度，具体安排须另行书面批准。"


def test_can_question_uses_cited_negative_action_wording() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "超过适用条件应发起申请，不得先办理后补手续。",
    )]
    citation_id = str(evidence[0].chunk_id)

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "answered",
            "不能直接补办，应通过申请流程处理。",
            (citation_id,),
        ),
        threshold=0.72,
    ).answer("事项已经发生，现在能直接补办吗？")

    assert answer.status == "answered"
    assert answer.text == "超过适用条件应发起申请，不得先办理后补手续。"


def test_answered_unspecified_final_consequence_is_forced_to_abstain() -> None:
    evidence = [result(
        "00000000-0000-0000-0000-000000000001",
        "业务招待费应事前审批，并随报销单提交审批记录。",
    )]
    citation_id = str(evidence[0].chunk_id)

    answer = AnswerService(
        retrieve=lambda _q: evidence,
        source_is_active=lambda _id: True,
        generate=lambda *_args, **_kwargs: ModelAnswer(
            "answered",
            "制度没有明确规定未事前审批的最终报销后果。",
            (citation_id,),
        ),
        threshold=0.72,
    ).answer("1200元招待费没有事前审批，最终后果是否有明确规定？")

    assert answer.status == "abstained"
    assert answer.text is None
    assert answer.refusal_reason == RefusalReason.LOW_CONFIDENCE
