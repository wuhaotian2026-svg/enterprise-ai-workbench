from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedBlock:
    text: str
    page: int | None
    heading_path: tuple[str, ...]
    location: str
    block_type: str
