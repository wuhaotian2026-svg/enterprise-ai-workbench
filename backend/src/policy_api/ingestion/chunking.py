from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from policy_api.ingestion.types import ParsedBlock


@dataclass(frozen=True)
class ChunkDraft:
    text: str
    page: int | None
    heading_path: tuple[str, ...]
    location: str
    block_type: str
    text_hash: str
    chunk_key: str


def _segments(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = [item for item in re.findall(r".*?[。！？；]|.+$", text) if item]
    result: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            result.append(sentence)
        else:
            result.extend(sentence[index:index + max_chars] for index in range(0, len(sentence), max_chars))
    return result


def chunk_blocks(blocks: list[ParsedBlock], *, max_chars: int) -> list[ChunkDraft]:
    if max_chars < 1:
        raise ValueError("max_chars_must_be_positive")
    chunks: list[ChunkDraft] = []
    for block_index, block in enumerate(blocks):
        for segment_index, text in enumerate(_segments(block.text, max_chars)):
            if not text:
                continue
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            identity = f"{block_index}:{segment_index}:{block.location}:{text_hash}"
            chunks.append(ChunkDraft(text=text, page=block.page, heading_path=block.heading_path,
                location=block.location, block_type=block.block_type, text_hash=text_hash,
                chunk_key=hashlib.sha256(identity.encode("utf-8")).hexdigest()))
    return chunks
