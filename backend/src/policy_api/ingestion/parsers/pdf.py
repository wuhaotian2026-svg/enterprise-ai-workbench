from __future__ import annotations

import io

from pypdf import PdfReader

from policy_api.ingestion.parsers.base import ParseError
from policy_api.ingestion.types import ParsedBlock


def parse_pdf(content: bytes) -> list[ParsedBlock]:
    try: reader = PdfReader(io.BytesIO(content))
    except Exception as exc: raise ParseError("pdf_invalid") from exc
    blocks = []
    try:
        for page_number, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            if text: blocks.append(ParsedBlock(text=text, page=page_number, heading_path=(), location=f"page:{page_number}", block_type="paragraph"))
    except Exception as exc: raise ParseError("pdf_invalid") from exc
    if not blocks: raise ParseError("empty_document")
    return blocks
