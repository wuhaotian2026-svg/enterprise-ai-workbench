from __future__ import annotations

import io

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from policy_api.ingestion.parsers.base import ParseError
from policy_api.ingestion.types import ParsedBlock


def parse_docx(content: bytes) -> list[ParsedBlock]:
    try: document = Document(io.BytesIO(content))
    except Exception as exc: raise ParseError("docx_invalid") from exc
    blocks: list[ParsedBlock] = []; headings: list[str] = []
    for index, item in enumerate(document.iter_inner_content(), 1):
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text: continue
            style = item.style.name if item.style else ""
            if style.startswith("Heading"):
                try: level = int(style.split()[-1])
                except ValueError: level = 1
                headings = headings[: level - 1] + [text]; kind = "heading"; path = tuple(headings)
            else:
                kind = "list_item" if style.startswith("List") else "paragraph"; path = tuple(headings)
            blocks.append(ParsedBlock(text=text, page=None, heading_path=path, location=f"body:{index}", block_type=kind))
        elif isinstance(item, Table):
            rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in item.rows]
            text = "\n".join(row for row in rows if row.strip(" |"))
            if text: blocks.append(ParsedBlock(text=text, page=None, heading_path=tuple(headings), location=f"body:{index}", block_type="table"))
    if not blocks: raise ParseError("empty_document")
    return blocks
