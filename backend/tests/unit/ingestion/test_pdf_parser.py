from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from policy_api.ingestion.parsers.base import ParseError
from policy_api.ingestion.parsers.pdf import parse_pdf


def blank_pdf(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count): writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO(); writer.write(buffer); return buffer.getvalue()


def test_pdf_parser_preserves_page_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        def __init__(self, text: str): self.text = text
        def extract_text(self) -> str: return self.text
    class Reader:
        pages = [Page("第一页制度"), Page("第二页流程")]
    monkeypatch.setattr("policy_api.ingestion.parsers.pdf.PdfReader", lambda _stream: Reader())
    blocks = parse_pdf(b"%PDF-test")
    assert [(block.page, block.text) for block in blocks] == [(1, "第一页制度"), (2, "第二页流程")]


def test_pdf_parser_rejects_empty_and_corrupt_pdf() -> None:
    with pytest.raises(ParseError, match="empty_document"):
        parse_pdf(blank_pdf(2))
    with pytest.raises(ParseError, match="pdf_invalid"):
        parse_pdf(b"not-a-pdf")
