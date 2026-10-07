from __future__ import annotations

import pytest

from policy_api.ingestion.parsers.base import ParseError
from policy_api.ingestion.parsers.text import parse_text


def test_text_parser_returns_paragraph_blocks_in_order() -> None:
    blocks = parse_text("第一条 休假申请。\n\n第二条 不得补签。".encode())
    assert [block.text for block in blocks] == ["第一条 休假申请。", "第二条 不得补签。"]
    assert [block.location for block in blocks] == ["paragraph:1", "paragraph:2"]


def test_text_parser_rejects_invalid_utf8_and_empty_body() -> None:
    with pytest.raises(ParseError, match="text_encoding_invalid"):
        parse_text(b"\xff\xfe")
    with pytest.raises(ParseError, match="empty_document"):
        parse_text(b" \n\n ")
