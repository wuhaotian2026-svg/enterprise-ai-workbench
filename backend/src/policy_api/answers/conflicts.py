from __future__ import annotations

import re

from policy_api.answers.schemas import ConflictDecision, EvidenceChunk


PATTERNS = {
    "money": re.compile(r"(\d+(?:\.\d+)?)\s*(?:\u5143|\u4eba\u6c11\u5e01)"),
    "days": re.compile(r"(\d+(?:\.\d+)?)\s*(?:\u4e2a\u5de5\u4f5c\u65e5|\u5de5\u4f5c\u65e5|\u5929|\u65e5)"),
    "percentage": re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|\uff05|\u767e\u5206\u4e4b)"),
}
ALLOW = ("\u53ef\u4ee5", "\u5141\u8bb8", "\u53ef\u4e88", "\u53ef\u62a5\u9500")
DENY = ("\u4e0d\u5f97", "\u7981\u6b62", "\u4e0d\u5141\u8bb8", "\u4e0d\u53ef", "\u4e0d\u4e88")
TOPICS = (
    "\u5e74\u5047", "\u75c5\u5047", "\u4e8b\u5047", "\u4f4f\u5bbf", "\u62a5\u9500",
    "\u5dee\u65c5", "\u91c7\u8d2d", "\u5408\u540c", "\u53d1\u7968", "\u8865\u5361",
)


def _fact_key(text: str, number_start: int) -> str:
    prefix = text[max(0, number_start - 16):number_start]
    clause = re.split(r"[\n\r,.;:!?\uff0c\u3002\uff1b\uff1a\uff01\uff1f]", prefix)[-1]
    normalized = re.sub(r"[\d\s]+", "", clause)
    prefix_text = text[:number_start]
    topic_positions = [(prefix_text.rfind(topic), topic) for topic in TOPICS]
    topic = max(topic_positions, default=(-1, ""))[1] if topic_positions else ""
    return topic + "|" + normalized[-16:]


def detect_conflicts(chunks: list[EvidenceChunk] | tuple[EvidenceChunk, ...]) -> ConflictDecision:
    categories: list[str] = []
    combined = [chunk.text for chunk in chunks]
    for category, pattern in PATTERNS.items():
        grouped: dict[str, set[str]] = {}
        for text in combined:
            for match in pattern.finditer(text):
                key = _fact_key(text, match.start())
                if key:
                    grouped.setdefault(key, set()).add(match.group(1))
        if any(len(values) > 1 for values in grouped.values()):
            categories.append(category)
    has_allow = any(term in text for text in combined for term in ALLOW)
    has_deny = any(term in text for text in combined for term in DENY)
    if has_allow and has_deny:
        categories.append("permission")
    return ConflictDecision(conflicting=bool(categories), categories=tuple(categories))
