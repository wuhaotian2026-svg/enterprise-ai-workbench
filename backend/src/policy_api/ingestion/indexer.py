from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from policy_api.ingestion.chunking import chunk_blocks
from policy_api.ingestion.types import ParsedBlock
from policy_api.models import Document, DocumentChunk, DocumentStatus, IngestionRun, IngestionStage, IngestionStatus


@dataclass(frozen=True)
class PreparedChunk:
    sequence: int
    text: str
    text_hash: str
    embedding: tuple[float, ...]
    page: int | None
    heading_path: tuple[str, ...]
    location: str


def prepare_chunks(blocks: list[ParsedBlock], *, embed: Callable[[list[str]], list[list[float]]],
                   batch_size: int, max_chars: int) -> list[PreparedChunk]:
    if batch_size < 1:
        raise ValueError("batch_size_must_be_positive")
    drafts = chunk_blocks(blocks, max_chars=max_chars)
    vectors: list[list[float]] = []
    for start in range(0, len(drafts), batch_size):
        batch = drafts[start:start + batch_size]
        result = embed([draft.text for draft in batch])
        if len(result) != len(batch):
            raise ValueError("embedding_count_mismatch")
        vectors.extend(result)
    return [PreparedChunk(sequence=index, text=draft.text, text_hash=draft.text_hash,
        embedding=tuple(vectors[index]), page=draft.page, heading_path=draft.heading_path,
        location=draft.location) for index, draft in enumerate(drafts)]


def persist_index_atomically(db: Session, document_id: UUID, run_id: UUID,
                             chunks: list[PreparedChunk]) -> None:
    try:
        document = db.get(Document, document_id)
        run = db.get(IngestionRun, run_id)
        if document is None or run is None or run.document_id != document_id:
            raise ValueError("index_target_not_found")
        for chunk in chunks:
            db.add(DocumentChunk(ingestion_run_id=run_id, document_id=document_id,
                sequence=chunk.sequence, heading_path=" > ".join(chunk.heading_path) or None,
                page_number=chunk.page, location=chunk.location, text=chunk.text,
                text_hash=chunk.text_hash, embedding=list(chunk.embedding)))
        db.flush()
        run.stage = IngestionStage.COMPLETED
        run.status = IngestionStatus.COMPLETED
        run.finished_at = datetime.now(timezone.utc)
        document.active_run_id = run_id
        document.status = DocumentStatus.ENABLED
        document.is_enabled = True
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        failed = db.get(IngestionRun, run_id)
        if failed is not None:
            failed.stage = IngestionStage.FAILED
            failed.status = IngestionStatus.FAILED
            failed.error_code = "index_persist_failed"
            failed.finished_at = datetime.now(timezone.utc)
            db.commit()
        raise
