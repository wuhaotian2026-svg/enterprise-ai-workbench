from __future__ import annotations

import importlib

import pytest


def _normalization():
    return importlib.import_module("policy_api.slot_extraction.normalization")


def test_original_exact_quote_has_original_span() -> None:
    normalization = _normalization()
    text = "我想申请年假，用于探亲"
    match = normalization.locate_source_quote(text, "用于探亲")
    assert match.match_kind == "original_exact"
    assert text[match.source_span.start : match.source_span.end] == "用于探亲"


def test_controlled_normalization_maps_back_to_original_span() -> None:
    normalization = _normalization()
    text = "需要日期２０２６．９．３０　人民币"
    match = normalization.locate_source_quote(text, "2026.9.30 人民币")
    assert match.match_kind == "controlled_normalized_exact"
    original = text[match.source_span.start : match.source_span.end]
    assert normalization.normalize_with_spans(original).text == "2026.9.30 人民币"


def test_duplicate_exact_quote_is_ambiguous_not_first_match() -> None:
    normalization = _normalization()
    with pytest.raises(normalization.SourceQuoteAmbiguous):
        normalization.locate_source_quote("年假，年假", "年假")


def test_duplicate_normalized_quote_is_ambiguous() -> None:
    normalization = _normalization()
    with pytest.raises(normalization.SourceQuoteAmbiguous):
        normalization.locate_source_quote("５００元和５００元", "500元")


def test_exact_quote_plus_normalized_equivalent_is_ambiguous() -> None:
    normalization = _normalization()
    with pytest.raises(normalization.SourceQuoteAmbiguous):
        normalization.locate_source_quote("500元和５００元", "500元")


def test_semantic_rewrite_is_never_used() -> None:
    normalization = _normalization()
    with pytest.raises(normalization.SourceQuoteNotFound):
        normalization.locate_source_quote("用于探亲", "回家探望亲属")


def test_whitespace_collapse_maps_to_complete_original_slice() -> None:
    normalization = _normalization()
    text = "金额  \t５００　元"
    match = normalization.locate_source_quote(text, "金额 500 元")
    assert text[match.source_span.start : match.source_span.end] == text


def test_combining_sequence_is_nfc_and_keeps_original_bounds() -> None:
    normalization = _normalization()
    text = "Cafe\u0301"
    normalized = normalization.normalize_with_spans(text)
    assert normalized.text == "Café"
    assert normalized.original_spans[-1] == normalization.SourceSpan(start=3, end=5)


def test_only_versioned_punctuation_is_mapped() -> None:
    normalization = _normalization()
    assert normalization.normalize_with_spans("．。／－–—：，").text == "../---:,"
    assert normalization.normalize_with_spans("！").text == "!"
