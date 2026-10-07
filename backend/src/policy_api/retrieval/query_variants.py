from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from policy_api.retrieval.errors import RetrievalError


_CONVERSATIONAL_PREFIX = re.compile(
    r"^(?:"
    r"请问|"
    r"请帮我(?:查|看)(?:一下)?|"
    r"麻烦(?:帮我)?(?:查|看|问)(?:一下)?|"
    r"我想(?:问|了解|咨询)(?:一下)?|"
    r"我(?:想)?咨询(?:一下)?|"
    r"想问(?:一下)?"
    r")[,，:：\s]*"
)
_FIRST_PERSON_PREFIX = re.compile(r"^我(?=\S{2,})")
_AMOUNT_CUES = ("多少钱", "金额", "费用", "报销", "补贴", "补助", "标准", "上限")
_DURATION_CUES = ("多少天", "几天", "期限", "多久", "什么时候", "提前")
_RATIO_CUES = ("比例", "百分之", "%", "％", "折算")
_YES_NO_CUES = ("是不是", "是否", "有没有", "能否", "可否", "能不能")
_FIXED_ENTITLEMENT_CUES = ("固定", "每月", "每周", "每天", "每年")


def normalize_query(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def strip_conversational_shell(value: str) -> str:
    focused = _CONVERSATIONAL_PREFIX.sub("", value, count=1)
    focused = _FIRST_PERSON_PREFIX.sub("", focused, count=1)
    return focused.strip(" ,，:：") or value


def _append_missing_terms(value: str, terms: tuple[str, ...]) -> str:
    missing = [term for term in terms if term not in value]
    return value if not missing else f"{value} {' '.join(missing)}"


def expand_fact_terms(value: str) -> str:
    expanded = value
    amount_question = any(cue in value for cue in _AMOUNT_CUES) or (
        "多少" in value and any(noun in value for noun in ("费", "钱", "补贴", "补助"))
    )
    if amount_question:
        expanded = _append_missing_terms(expanded, ("标准", "上限", "补助", "报销"))
    if any(cue in value for cue in _DURATION_CUES):
        expanded = _append_missing_terms(expanded, ("期限", "天数", "工作日", "自然日"))
    if any(cue in value for cue in _RATIO_CUES):
        expanded = _append_missing_terms(expanded, ("比例", "折算"))
    if (
        any(cue in value for cue in _YES_NO_CUES)
        and any(cue in value for cue in _FIXED_ENTITLEMENT_CUES)
    ):
        expanded = _append_missing_terms(expanded, ("未规定", "固定额度", "批准"))
    return expanded


@dataclass(frozen=True, slots=True)
class QueryVariantBuilder:
    max_count: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.max_count <= 3:
            raise ValueError("query_variant_max_count_invalid")

    def build(self, question: str) -> tuple[str, ...]:
        original = normalize_query(question)
        if not original:
            raise RetrievalError("empty_question")
        focused = strip_conversational_shell(original)
        expanded = expand_fact_terms(focused)
        return tuple(
            dict.fromkeys(
                value for value in (original, focused, expanded) if value
            )
        )[: self.max_count]
