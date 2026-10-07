from __future__ import annotations

from policy_api.ingestion.parsers.base import ParseError
from policy_api.ingestion.types import ParsedBlock


def parse_text(content: bytes) -> list[ParsedBlock]:
    try: text = content.decode("utf-8")
    except UnicodeDecodeError as exc: raise ParseError("text_encoding_invalid") from exc
    paragraphs = [part.strip() for part in text.replace("\r\n", "\n").split("\n\n") if part.strip()]
    if not paragraphs: raise ParseError("empty_document")
    return [ParsedBlock(text=value, page=None, heading_path=(), location=f"paragraph:{index}", block_type="paragraph") for index, value in enumerate(paragraphs, 1)]
