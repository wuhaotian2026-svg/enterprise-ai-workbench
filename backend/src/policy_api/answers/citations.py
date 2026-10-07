from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from policy_api.answers.schemas import EvidenceChunk


FACT = re.compile(
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:元|人民币|天|日|个工作日|%|％)"
    r"|\d{4}年\d{1,2}月\d{1,2}日"
    r"|百分之\s*[零一二三四五六七八九十百千万\d]+"
)

NUMBER = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
SMALL_CHINESE_QUANTITY = re.compile(
    r"(?P<number>[一二三四五六七八九十两])(?P<unit>个?工作日|天|日|晚|次|月)"
)
SMALL_CHINESE_NUMBER_VALUES = {
    "一": "1",
    "二": "2",
    "两": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
    "十": "10",
}
OPERAND_UNIT = r"元/人/晚|元/人/天|元/人|元/晚|元/天|元/月|人民币|元|个工作日|天|日|晚|月|%|％"
RESULT_UNIT = r"人民币|元|个工作日|天|日|%|％"
DERIVATION = re.compile(
    rf"(?P<left>{NUMBER.pattern})\s*"
    rf"(?P<left_unit>{OPERAND_UNIT})?\s*"
    r"(?P<operator>[×xX*+＋\-−÷/])\s*"
    rf"(?P<right>{NUMBER.pattern})\s*"
    rf"(?P<right_unit>{OPERAND_UNIT})?\s*"
    r"(?:=|＝)\s*"
    rf"(?P<result>{NUMBER.pattern})\s*"
    rf"(?P<result_unit>{RESULT_UNIT})"
)
RESULT_FIRST_DERIVATION = re.compile(
    rf"(?P<result>{NUMBER.pattern})\s*"
    rf"(?P<result_unit>{RESULT_UNIT})\s*[（(]\s*"
    rf"(?P<left>{NUMBER.pattern})\s*"
    rf"(?P<left_unit>{OPERAND_UNIT})?\s*"
    r"(?P<operator>[×xX*+＋\-−÷/])\s*"
    rf"(?P<right>{NUMBER.pattern})\s*"
    rf"(?P<right_unit>{OPERAND_UNIT})?\s*[）)]"
)


@dataclass(frozen=True)
class CitationDecision:
    valid: bool
    reason: str | None


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("％", "%").replace(",", "")


def _canonical_chinese_quantity_facts(text: str) -> set[str]:
    return {
        SMALL_CHINESE_NUMBER_VALUES[match.group("number")] + match.group("unit")
        for match in SMALL_CHINESE_QUANTITY.finditer(text)
    }


def _derived_facts(answer: str, *, question: str, supporting_text: str) -> set[str]:
    supported_numbers = {
        _normalize(match.group(0))
        for match in NUMBER.finditer(f"{question}\n{supporting_text}")
    }
    supported_numbers.update(
        SMALL_CHINESE_NUMBER_VALUES[match.group("number")]
        for match in SMALL_CHINESE_QUANTITY.finditer(question)
    )
    derived: set[str] = set()
    matches = (*DERIVATION.finditer(answer), *RESULT_FIRST_DERIVATION.finditer(answer))
    for match in matches:
        left_text = _normalize(match.group("left"))
        right_text = _normalize(match.group("right"))
        if left_text not in supported_numbers or right_text not in supported_numbers:
            continue
        try:
            left = Decimal(left_text)
            right = Decimal(right_text)
            result = Decimal(_normalize(match.group("result")))
            operator = match.group("operator")
            expected = {
                "×": lambda: left * right,
                "x": lambda: left * right,
                "X": lambda: left * right,
                "*": lambda: left * right,
                "+": lambda: left + right,
                "＋": lambda: left + right,
                "-": lambda: left - right,
                "−": lambda: left - right,
                "÷": lambda: left / right,
                "/": lambda: left / right,
            }[operator]()
        except (InvalidOperation, ZeroDivisionError):
            continue
        if result == expected:
            derived.add(_normalize(match.group("result") + match.group("result_unit")))
    return derived


def validate_citations(answer: str, citation_ids: tuple[str, ...],
                       evidence: list[EvidenceChunk] | tuple[EvidenceChunk, ...],
                       *, question: str = "") -> CitationDecision:
    by_id = {chunk.chunk_id: chunk for chunk in evidence}
    if not citation_ids or any(identifier not in by_id for identifier in citation_ids):
        return CitationDecision(False, "citation_not_found")
    selected = [by_id[identifier] for identifier in citation_ids]
    if any(not chunk.source_active for chunk in selected):
        return CitationDecision(False, "citation_source_inactive")
    supporting_text = _normalize("\n".join(chunk.text for chunk in selected))
    question_facts = {_normalize(fact) for fact in FACT.findall(question)}
    question_facts.update(_canonical_chinese_quantity_facts(question))
    derived_facts = _derived_facts(
        answer,
        question=question,
        supporting_text="\n".join(chunk.text for chunk in selected),
    )
    for fact in FACT.findall(answer):
        normalized_fact = _normalize(fact)
        if (
            normalized_fact not in question_facts
            and normalized_fact not in supporting_text
            and normalized_fact not in derived_facts
        ):
            return CitationDecision(False, "unsupported_numeric_fact")
    return CitationDecision(True, None)
