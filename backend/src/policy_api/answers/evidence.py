from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from policy_api.answers.conflicts import detect_conflicts
from policy_api.answers.schemas import EvidenceChunk, EvidenceDecision
from policy_api.models import RefusalReason
from policy_api.retrieval.types import RetrievalResult


REQUIRED_FACTS = (
    (("多少钱", "金额", "费用", "上限"), re.compile(r"\d+(?:\.\d+)?\s*(?:元|人民币)")),
    (("多少天", "几天", "期限", "多久"), re.compile(r"\d+(?:\.\d+)?\s*(?:天|日|个工作日)")),
    (("比例", "百分之", "%", "％"), re.compile(r"(?:\d+(?:\.\d+)?\s*(?:%|％)|百分之\s*\d+)")),
)
GENERIC_DOCUMENT_NAME_TERMS = (
    "制度",
    "规定",
    "办法",
    "管理",
    "政策",
    "文档",
)
EXPENSE_OBJECT = re.compile(r"发票|票据|报销|费用(?:审批|额度)")
PROCUREMENT_OBJECT = re.compile(r"采购|订单|报价|供应商")
EXPENSE_DOCUMENT = re.compile(r"费用|报销")
PROCUREMENT_DOCUMENT = re.compile(r"采购")


def required_facts_present(question: str, evidence_text: str) -> bool:
    return all(
        not any(cue in question for cue in cues) or fact_pattern.search(evidence_text)
        for cues, fact_pattern in REQUIRED_FACTS
    )


def _channel_score(item: RetrievalResult) -> float:
    score = max(item.vector_score or 0.0, item.lexical_score or 0.0)
    return min(1.0, max(0.0, score))


def _is_markdown_heading_only(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines) == 1 and re.fullmatch(
        r"\s{0,3}#{1,6}\s+\S.*", lines[0]
    ) is not None


def _document_name_features(value: str) -> set[str]:
    normalized = re.sub(r"\.[a-z0-9]{1,8}$", "", value.casefold())
    for term in GENERIC_DOCUMENT_NAME_TERMS:
        normalized = normalized.replace(term, "")
    normalized = "".join(re.findall(r"[\u4e00-\u9fff]|[a-z0-9]+", normalized))
    if not normalized:
        return set()
    if len(normalized) == 1:
        return {normalized}
    return {
        normalized[index : index + 2]
        for index in range(len(normalized) - 1)
    }


def _document_name_relevance(question: str, document_name: str) -> float:
    document_features = _document_name_features(document_name)
    if not document_features:
        return 0.0
    question_features = _document_name_features(question)
    return len(question_features & document_features) / len(document_features)


def _question_text_relevance(question: str, text: str) -> float:
    question_features = _document_name_features(question)
    if not question_features:
        return 0.0
    return len(question_features & _document_name_features(text)) / len(
        question_features
    )


@dataclass(frozen=True, slots=True)
class EvidenceSelector:
    semantic_threshold: float
    lexical_threshold: float
    dual_channel_minimum: float
    max_chunks: int

    def select(
        self,
        question: str,
        candidates: Sequence[RetrievalResult],
        source_is_active: Callable[[UUID], bool],
    ) -> EvidenceDecision:
        if not candidates:
            return EvidenceDecision(False, RefusalReason.NO_EVIDENCE, 0.0, ())

        relevant_results = [
            item
            for item in candidates
            if not _is_markdown_heading_only(item.text)
            and (
                (
                    item.vector_score is not None
                    and item.vector_score >= self.semantic_threshold
                )
                or (
                    item.lexical_score is not None
                    and item.lexical_score >= self.lexical_threshold
                )
                or (
                    item.vector_score is not None
                    and item.lexical_score is not None
                    and min(item.vector_score, item.lexical_score)
                    >= self.dual_channel_minimum
                )
            )
        ]
        selected_results: list[RetrievalResult] = []
        if relevant_results:
            expense_object = bool(EXPENSE_OBJECT.search(question))
            procurement_object = bool(PROCUREMENT_OBJECT.search(question))
            direct_document_pattern = (
                EXPENSE_DOCUMENT
                if expense_object and not procurement_object
                else PROCUREMENT_DOCUMENT
                if procurement_object and not expense_object
                else None
            )
            if direct_document_pattern is not None:
                scoped_results = [
                    item
                    for item in relevant_results
                    if direct_document_pattern.search(item.document_name)
                ]
                if scoped_results:
                    relevant_results = scoped_results
            document_name_scores: dict[UUID, float] = {}
            for item in relevant_results:
                document_name_scores[item.document_id] = max(
                    document_name_scores.get(item.document_id, 0.0),
                    _document_name_relevance(question, item.document_name),
                )
            if max(document_name_scores.values(), default=0.0) > 0:
                matched_document_ids = {
                    document_id
                    for document_id, score in document_name_scores.items()
                    if score > 0
                }
                if len(matched_document_ids) == 1:
                    title_document_id = next(iter(matched_document_ids))
                    first_result = relevant_results[0]
                    if (
                        first_result.document_id != title_document_id
                        and _question_text_relevance(
                            question, first_result.text
                        )
                        > document_name_scores[title_document_id]
                    ):
                        matched_document_ids = {first_result.document_id}
                selected_results = [
                    item
                    for item in relevant_results
                    if item.document_id in matched_document_ids
                ][: self.max_chunks]
            else:
                relevant_results = sorted(
                    relevant_results,
                    key=lambda item: _question_text_relevance(question, item.text),
                    reverse=True,
                )
                primary_document_id = relevant_results[0].document_id
                original_budget = relevant_results[: self.max_chunks]
                document_counts: dict[UUID, int] = {}
                for item in original_budget:
                    document_counts[item.document_id] = (
                        document_counts.get(item.document_id, 0) + 1
                    )
                protected_competing = [
                    item
                    for item in original_budget
                    if item.document_id != primary_document_id
                    and document_counts[item.document_id] >= 2
                ]
                primary_capacity = self.max_chunks - len(protected_competing)
                selected_results = [
                    item
                    for item in relevant_results
                    if item.document_id == primary_document_id
                ][:primary_capacity]
                selected_ids = {item.chunk_id for item in selected_results}
                selected_results.extend(protected_competing)
                selected_ids.update(item.chunk_id for item in protected_competing)
                selected_results.extend(
                    item
                    for item in relevant_results
                    if item.chunk_id not in selected_ids
                )
                selected_results = selected_results[: self.max_chunks]
        score = max((_channel_score(item) for item in selected_results), default=0.0)
        if not selected_results:
            return EvidenceDecision(False, RefusalReason.LOW_CONFIDENCE, score, ())

        chunks = tuple(
            EvidenceChunk(
                chunk_id=str(item.chunk_id),
                text=item.text,
                score=_channel_score(item),
                source_active=source_is_active(item.document_id),
            )
            for item in selected_results
        )
        if any(not chunk.source_active for chunk in chunks):
            return EvidenceDecision(
                False, RefusalReason.CORPUS_UNAVAILABLE, score, ()
            )
        evidence_text = "\n".join(chunk.text for chunk in chunks)
        if not required_facts_present(question, evidence_text):
            return EvidenceDecision(False, RefusalReason.LOW_CONFIDENCE, score, ())
        if detect_conflicts(chunks).conflicting:
            return EvidenceDecision(
                False, RefusalReason.CONFLICTING_EVIDENCE, score, ()
            )
        return EvidenceDecision(True, None, score, chunks)


def evaluate_evidence(question: str, chunks: list[EvidenceChunk], *, threshold: float) -> EvidenceDecision:
    if not chunks:
        return EvidenceDecision(False, RefusalReason.NO_EVIDENCE, 0.0, ())
    if any(not chunk.source_active for chunk in chunks):
        return EvidenceDecision(False, RefusalReason.CORPUS_UNAVAILABLE, 0.0, ())
    score = max(chunk.score for chunk in chunks)
    selected = tuple(chunk for chunk in chunks if chunk.score >= threshold)
    if score < threshold or not selected:
        return EvidenceDecision(False, RefusalReason.LOW_CONFIDENCE, score, ())
    evidence_text = "\n".join(chunk.text for chunk in selected)
    if not required_facts_present(question, evidence_text):
        return EvidenceDecision(False, RefusalReason.LOW_CONFIDENCE, score, ())
    if detect_conflicts(selected).conflicting:
        return EvidenceDecision(False, RefusalReason.CONFLICTING_EVIDENCE, score, ())
    return EvidenceDecision(True, None, score, selected)
