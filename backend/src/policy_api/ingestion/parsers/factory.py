from __future__ import annotations

from collections.abc import Callable

from policy_api.ingestion.parsers.docx import parse_docx
from policy_api.ingestion.parsers.pdf import parse_pdf
from policy_api.ingestion.parsers.text import parse_text
from policy_api.ingestion.types import ParsedBlock


PARSERS: dict[str, Callable[[bytes], list[ParsedBlock]]] = {".pdf": parse_pdf, ".docx": parse_docx, ".txt": parse_text}


def parser_for(extension: str) -> Callable[[bytes], list[ParsedBlock]]:
    try: return PARSERS[extension.lower()]
    except KeyError as exc: raise ValueError("unsupported_document_type") from exc
