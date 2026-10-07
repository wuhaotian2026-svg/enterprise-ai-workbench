from __future__ import annotations

import re
from collections.abc import Callable
from uuid import UUID

from policy_api.answers.citations import validate_citations
from policy_api.answers.evidence import EvidenceSelector
from policy_api.answers.llm_client import ModelAnswer
from policy_api.answers.prompt import (
    build_messages,
    build_reconsideration_messages,
    build_repair_messages,
)
from policy_api.answers.schemas import AnswerOutcome, EvidenceChunk
from policy_api.models import RefusalReason
from policy_api.retrieval.types import RetrievalResult


ABSTENTION_LANGUAGE = (
    "应拒答", "必须拒答", "制度未规定", "政策未规定", "无法确认",
    "不能确认", "无法回答", "不能回答", "建议咨询", "未明确规定",
    "没有明确规定",
)
UNRESOLVED_USER_FACT = re.compile(
    r"请(?:提供|补充|确认|告知)|需(?:要)?(?:根据|提供|补充|确认)您的|"
    r"需要您(?:提供|补充|确认)|需补充"
)
EXPLICIT_CONDITIONAL_RULE = re.compile(
    r"若|如果|否则|取决于|视[^。；]{0,20}而定|当[^。；]{0,40}时"
)
CALCULATION_INTENT = re.compile(
    r"金额|多少钱|总价|合计|总额|计算|算一下|多少(?:天|晚|次|元)|费用|多少"
)
PERSONAL_SCENARIO = re.compile(r"我|本人|本次|这次|此次|当前")
UNRESOLVED_CONDITIONAL_CALCULATION = re.compile(
    r"(?:按|根据|取决于)[^。；\n]{0,48}"
    r"(?:日期|年限|比例|类别|档位|数量|天数|晚数)"
    r"[^。；\n]{0,24}(?:折算|计算|确定|适用|而定)"
)
KNOWN_QUANTITY_CLARIFICATION_SLOTS = (
    (
        re.compile(r"(?:住宿|住了|驻场)?[^，。！？?\n]{0,12}[0-9零一二三四五六七八九十两]+晚"),
        re.compile(r"(?:住宿|住)[^，。！？?\n]{0,12}(?:几|多少)(?:个)?晚|(?:几|多少)(?:个)?晚"),
    ),
    (
        re.compile(r"(?:全天)?(?:供餐|包餐|供饭)[^，。！？?\n]{0,12}[0-9零一二三四五六七八九十两]+天"),
        re.compile(r"(?:全天)?(?:供餐|包餐|供饭)[^，。！？?\n]{0,12}(?:几|多少)(?:个)?天"),
    ),
)
CALCULABLE_POLICY_VALUE = re.compile(r"\d+(?:\.\d+)?\s*(?:元|%|％)")
ALTERNATIVE_CALCULABLE_VALUE = re.compile(
    r"(?:其他|分别|不同)[^，,。；！？\n]{0,32}\d+(?:\.\d+)?\s*(?:元|%|％)"
)
NEGATIVE_RULE = re.compile(r"未规定|没有|不存在|不得|不允许|不可以|不能|禁止|规避|无固定")
DIRECT_PERMISSION_DENIAL = re.compile(r"不可以|不允许|不能")
REQUIRED_ACTION = re.compile(r"须|需|应|批准|申请|证明|报备|提交")
YES_NO_ENTITLEMENT = re.compile(
    r"是否(?:允许|可以)|能否|可否|是不是|允许|可以|能不能|有没有|"
    r"能[^，。！？?\n]{0,32}[吗么]"
)
QUANTIFIED_CLAIM = re.compile(
    r"\d+(?:\.\d+)?(?:个?工作日|个?自然日|天|晚|次|元|%|％)?"
    r"|每(?:月|周|天|年)"
)


class AnswerService:
    def __init__(
        self,
        *,
        retrieve: Callable[[str], list[RetrievalResult]],
        source_is_active: Callable[[UUID], bool],
        generate: Callable[..., ModelAnswer],
        selector: EvidenceSelector | None = None,
        threshold: float | None = None,
    ) -> None:
        self.retrieve = retrieve
        self.source_is_active = source_is_active
        self.generate = generate
        if selector is None:
            if threshold is None:
                raise ValueError("evidence_selector_required")
            selector = EvidenceSelector(
                semantic_threshold=threshold,
                lexical_threshold=threshold,
                dual_channel_minimum=threshold,
                max_chunks=12,
            )
        self.selector = selector

    @staticmethod
    def _abstained(
        reason: RefusalReason,
        score: float,
    ) -> AnswerOutcome:
        return AnswerOutcome("abstained", None, reason, score, (), ())

    @staticmethod
    def _contains_unsupported_abstention_language(
        answer: str,
        citations: tuple[str, ...],
        evidence: tuple[EvidenceChunk, ...],
    ) -> bool:
        normalized_answer = re.sub(r"\s+", "", answer).casefold()
        by_id = {chunk.chunk_id: chunk for chunk in evidence}
        normalized_evidence = re.sub(
            r"\s+",
            "",
            "\n".join(
                by_id[identifier].text
                for identifier in citations
                if identifier in by_id
            ),
        ).casefold()
        return any(
            (normalized_marker := re.sub(r"\s+", "", marker).casefold())
            in normalized_answer
            and normalized_marker not in normalized_evidence
            for marker in ABSTENTION_LANGUAGE
        )

    @staticmethod
    def _has_conditional_rule(evidence: tuple[EvidenceChunk, ...]) -> bool:
        policy_text = "\n".join(chunk.text for chunk in evidence)
        sentences = re.split(
            r"[。！？\n]+",
            policy_text,
        )
        for sentence in sentences:
            if (
                EXPLICIT_CONDITIONAL_RULE.search(sentence)
                and CALCULABLE_POLICY_VALUE.search(sentence)
            ):
                return True
            for clause in re.split(r"[；;]+", sentence):
                if ALTERNATIVE_CALCULABLE_VALUE.search(clause):
                    return True
                if len(CALCULABLE_POLICY_VALUE.findall(clause)) >= 2:
                    return True
        return False

    @staticmethod
    def _should_reconsider(
        question: str,
        evidence: tuple[EvidenceChunk, ...],
    ) -> bool:
        policy_text = "\n".join(chunk.text for chunk in evidence)
        calculable_branch = (
            bool(CALCULATION_INTENT.search(question))
            and AnswerService._has_conditional_rule(evidence)
        )
        explicit_negative_entitlement = bool(
            YES_NO_ENTITLEMENT.search(question) and NEGATIVE_RULE.search(policy_text)
        )
        return calculable_branch or explicit_negative_entitlement

    @staticmethod
    def _contains_unresolved_user_fact(status: str, answer: str) -> bool:
        return status == "answered" and bool(UNRESOLVED_USER_FACT.search(answer))

    @staticmethod
    def _contains_unresolved_conditional_calculation(
        question: str,
        status: str,
        answer: str,
    ) -> bool:
        return bool(
            status == "answered"
            and PERSONAL_SCENARIO.search(question)
            and CALCULATION_INTENT.search(question)
            and UNRESOLVED_CONDITIONAL_CALCULATION.search(answer)
        )

    @staticmethod
    def _clarification_repeats_known_user_fact(
        question: str,
        status: str,
        clarification_questions: tuple[str, ...],
    ) -> bool:
        if status != "needs_clarification":
            return False
        return any(
            known_fact.search(question)
            and any(missing_fact.search(item) for item in clarification_questions)
            for known_fact, missing_fact in KNOWN_QUANTITY_CLARIFICATION_SLOTS
        )

    @staticmethod
    def _supported_negative_rule_answer(
        question: str,
        answer: str,
        citations: tuple[str, ...],
        evidence: tuple[EvidenceChunk, ...],
    ) -> str:
        if not YES_NO_ENTITLEMENT.search(question) or not NEGATIVE_RULE.search(answer):
            return answer

        by_id = {chunk.chunk_id: chunk for chunk in evidence}
        cited_text = "\n".join(
            by_id[identifier].text
            for identifier in citations
            if identifier in by_id
        )
        sentences = [
            sentence.strip()
            for sentence in re.findall(r"[^。！？\n]+[。！？]?", cited_text)
            if sentence.strip()
        ]
        negative_indexes = [
            index for index, sentence in enumerate(sentences)
            if NEGATIVE_RULE.search(sentence)
        ]
        if not negative_indexes:
            return answer

        question_chars = set(re.sub(r"\s+", "", question))
        best_index = max(
            negative_indexes,
            key=lambda index: len(
                question_chars & set(re.sub(r"\s+", "", sentences[index]))
            ),
        )
        supported_sentences = [sentences[best_index]]
        if (
            best_index + 1 < len(sentences)
            and REQUIRED_ACTION.search(sentences[best_index + 1])
        ):
            supported_sentences.append(sentences[best_index + 1])
        return "".join(supported_sentences)

    def answer(self, question: str) -> AnswerOutcome:
        retrieved = self.retrieve(question)
        decision = self.selector.select(
            question, retrieved, source_is_active=self.source_is_active
        )
        if not decision.allowed:
            return self._abstained(
                decision.reason or RefusalReason.LOW_CONFIDENCE,
                decision.score,
            )

        generated = self.generate(
            build_messages(question, decision.selected_chunks),
            allowed_citation_ids={chunk.chunk_id for chunk in decision.selected_chunks},
        )
        if generated.status == "abstained":
            if not self._should_reconsider(question, decision.selected_chunks):
                return self._abstained(RefusalReason.LOW_CONFIDENCE, decision.score)
            generated = self.generate(
                build_reconsideration_messages(question, decision.selected_chunks),
                allowed_citation_ids={
                    chunk.chunk_id for chunk in decision.selected_chunks
                },
            )
            if generated.status == "abstained":
                return self._abstained(RefusalReason.LOW_CONFIDENCE, decision.score)

        answer_text = self._supported_negative_rule_answer(
            question,
            generated.answer or "",
            generated.citations,
            decision.selected_chunks,
        )
        citation_decision = validate_citations(
            answer_text, generated.citations, decision.selected_chunks,
            question=question,
        )
        unresolved_user_fact = self._contains_unresolved_user_fact(
            generated.status,
            generated.answer or "",
        )
        unresolved_conditional_calculation = (
            self._contains_unresolved_conditional_calculation(
                question,
                generated.status,
                generated.answer or "",
            )
        )
        repeated_known_user_fact = self._clarification_repeats_known_user_fact(
            question,
            generated.status,
            generated.clarification_questions,
        )
        if (
            not citation_decision.valid
            or unresolved_user_fact
            or unresolved_conditional_calculation
            or repeated_known_user_fact
        ):
            validation_reason = (
                "clarification_repeats_known_user_fact"
                if repeated_known_user_fact
                else (
                    "answered_contains_unresolved_user_fact"
                    if unresolved_user_fact
                    else (
                        "answered_contains_unresolved_conditional_calculation"
                        if unresolved_conditional_calculation
                        else citation_decision.reason or "citation_validation_failed"
                    )
                )
            )
            repaired = self.generate(
                build_repair_messages(
                    question=question,
                    chunks=decision.selected_chunks,
                    previous_answer=generated.answer or "",
                    previous_citations=generated.citations,
                    validation_reason=validation_reason,
                    previous_status=generated.status,
                    previous_clarification_questions=generated.clarification_questions,
                ),
                allowed_citation_ids={
                    chunk.chunk_id for chunk in decision.selected_chunks
                },
            )
            if repaired.status == "abstained":
                return self._abstained(
                    RefusalReason.CITATION_VALIDATION_FAILED, decision.score
                )
            repaired_citation_decision = validate_citations(
                repaired.answer or "",
                repaired.citations,
                decision.selected_chunks,
                question=question,
            )
            if (
                not repaired_citation_decision.valid
                or self._contains_unresolved_user_fact(
                    repaired.status,
                    repaired.answer or "",
                )
                or self._contains_unresolved_conditional_calculation(
                    question,
                    repaired.status,
                    repaired.answer or "",
                )
                or self._clarification_repeats_known_user_fact(
                    question,
                    repaired.status,
                    repaired.clarification_questions,
                )
            ):
                return self._abstained(
                    RefusalReason.CITATION_VALIDATION_FAILED, decision.score
                )
            generated = repaired
            answer_text = self._supported_negative_rule_answer(
                question,
                generated.answer or "",
                generated.citations,
                decision.selected_chunks,
            )

        if generated.status == "answered" and self._contains_unsupported_abstention_language(
            answer_text, generated.citations, decision.selected_chunks
        ):
            return self._abstained(RefusalReason.LOW_CONFIDENCE, decision.score)

        clarification_questions = (
            generated.clarification_questions
            if generated.status == "needs_clarification"
            else ()
        )
        return AnswerOutcome(
            generated.status,
            answer_text,
            None,
            decision.score,
            generated.citations,
            clarification_questions,
        )
