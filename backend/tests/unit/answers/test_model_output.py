from __future__ import annotations

import pytest

from policy_api.answers.model_output import (
    ModelAnswerError,
    parse_model_answer,
)


def test_model_output_accepts_three_closed_shapes() -> None:
    answered = parse_model_answer(
        {"status": "answered", "answer": "  上限500元。 ", "citations": ["c1"]},
        allowed_citation_ids={"c1"},
    )
    clarified = parse_model_answer(
        {
            "status": "needs_clarification",
            "answer": "其他城市住宿标准为350元/晚。",
            "citations": ["c1"],
            "clarification_questions": [
                " 本次适用哪一城市档位？ ",
                "本次适用哪一城市档位？",
                "住宿几晚？",
            ],
        },
        allowed_citation_ids={"c1"},
    )
    abstained = parse_model_answer(
        {"status": "abstained", "answer": None, "citations": []},
        allowed_citation_ids={"c1"},
    )

    assert answered.answer == "上限500元。"
    assert answered.citations == ("c1",)
    assert clarified.status == "needs_clarification"
    assert clarified.clarification_questions == (
        "本次适用哪一城市档位？",
        "住宿几晚？",
    )
    assert abstained.answer is None and abstained.citations == ()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "needs_clarification",
            "answer": "规则",
            "citations": ["c1"],
            "clarification_questions": [],
        },
        {
            "status": "answered",
            "answer": "规则",
            "citations": ["c1"],
            "reason": "leak",
        },
        {"status": "abstained", "answer": "部分回答", "citations": []},
        {"status": "answered", "answer": "规则", "citations": ["outside"]},
        {"status": "answered", "answer": "   ", "citations": ["c1"]},
    ],
)
def test_model_output_rejects_invalid_or_extra_fields(payload: object) -> None:
    with pytest.raises(ModelAnswerError, match="model_output_invalid"):
        parse_model_answer(payload, allowed_citation_ids={"c1"})


@pytest.mark.parametrize(
    "question",
    [
        "请提供身份证号。",
        "你的银行卡号是什么？",
        "请告诉我登录密码。",
        "请粘贴 access token。",
        "请提供 API 密钥。",
    ],
)
def test_clarification_rejects_unnecessary_sensitive_questions(question: str) -> None:
    with pytest.raises(ModelAnswerError, match="model_output_invalid"):
        parse_model_answer(
            {
                "status": "needs_clarification",
                "answer": "需要补充信息。",
                "citations": ["c1"],
                "clarification_questions": [question],
            },
            allowed_citation_ids={"c1"},
        )


def test_clarification_allows_required_non_sensitive_business_facts() -> None:
    parsed = parse_model_answer(
        {
            "status": "needs_clarification",
            "answer": "现有制度提供条件标准。",
            "citations": ["c1"],
            "clarification_questions": [
                "入职日期是什么？",
                "本次住宿晚数是多少？",
                "主办方是否供餐？",
            ],
        },
        allowed_citation_ids={"c1"},
    )

    assert len(parsed.clarification_questions) == 3


@pytest.mark.parametrize("question", ["", "x" * 201])
def test_clarification_question_length_is_bounded(question: str) -> None:
    with pytest.raises(ModelAnswerError, match="model_output_invalid"):
        parse_model_answer(
            {
                "status": "needs_clarification",
                "answer": "规则",
                "citations": ["c1"],
                "clarification_questions": [question],
            },
            allowed_citation_ids={"c1"},
        )
