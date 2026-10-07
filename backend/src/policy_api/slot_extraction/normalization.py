from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal

from policy_api.slot_extraction.errors import (
    SourceQuoteAmbiguous,
    SourceQuoteNotFound,
)


NORMALIZATION_VERSION = "source-normalization-v1"

_PUNCTUATION_MAP = {
    "。": ".",
    "–": "-",
    "—": "-",
}


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class NormalizedText:
    text: str
    original_spans: tuple[SourceSpan, ...]


@dataclass(frozen=True, slots=True)
class SourceQuoteMatch:
    source_span: SourceSpan
    match_kind: Literal["original_exact", "controlled_normalized_exact"]
    normalization_version: Literal["source-normalization-v1"] = NORMALIZATION_VERSION


def _map_character(character: str) -> str:
    codepoint = ord(character)
    if 0xFF01 <= codepoint <= 0xFF5E:
        return chr(codepoint - 0xFEE0)
    return _PUNCTUATION_MAP.get(character, character)


def normalize_with_spans(text: str) -> NormalizedText:
    normalized: list[str] = []
    spans: list[SourceSpan] = []
    index = 0
    while index < len(text):
        start = index
        index += 1
        while index < len(text) and unicodedata.combining(text[index]):
            index += 1
        original_span = SourceSpan(start=start, end=index)
        group = unicodedata.normalize("NFC", text[start:index])
        for group_character in group:
            mapped = _map_character(group_character)
            for character in mapped:
                if character.isspace():
                    if normalized and normalized[-1] == " ":
                        previous = spans[-1]
                        spans[-1] = SourceSpan(start=previous.start, end=original_span.end)
                    else:
                        normalized.append(" ")
                        spans.append(original_span)
                else:
                    normalized.append(character)
                    spans.append(original_span)
    return NormalizedText(text="".join(normalized), original_spans=tuple(spans))


def _occurrences(text: str, needle: str) -> list[int]:
    if not needle:
        return []
    starts: list[int] = []
    offset = 0
    while True:
        found = text.find(needle, offset)
        if found < 0:
            return starts
        starts.append(found)
        offset = found + 1


def locate_source_quote(current_user_turn_text: str, source_quote: str) -> SourceQuoteMatch:
    exact_starts = _occurrences(current_user_turn_text, source_quote)
    if len(exact_starts) > 1:
        raise SourceQuoteAmbiguous()
    normalized_text = normalize_with_spans(current_user_turn_text)
    normalized_quote = normalize_with_spans(source_quote)
    normalized_starts = _occurrences(normalized_text.text, normalized_quote.text)
    if len(normalized_starts) > 1:
        raise SourceQuoteAmbiguous()
    if len(exact_starts) == 1:
        start = exact_starts[0]
        return SourceQuoteMatch(
            source_span=SourceSpan(start=start, end=start + len(source_quote)),
            match_kind="original_exact",
        )

    if not normalized_starts:
        raise SourceQuoteNotFound()

    normalized_start = normalized_starts[0]
    normalized_end = normalized_start + len(normalized_quote.text)
    original_start = normalized_text.original_spans[normalized_start].start
    original_end = normalized_text.original_spans[normalized_end - 1].end
    original_slice = current_user_turn_text[original_start:original_end]
    if normalize_with_spans(original_slice).text != normalized_quote.text:
        raise SourceQuoteNotFound()
    return SourceQuoteMatch(
        source_span=SourceSpan(start=original_start, end=original_end),
        match_kind="controlled_normalized_exact",
    )
