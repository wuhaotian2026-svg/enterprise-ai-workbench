from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from policy_api.models import Document, DocumentChunk, IngestionRun


def find_by_hash(db: Session, sha256: str) -> Document | None:
    return db.scalar(select(Document).where(Document.sha256 == sha256))


def get_document(db: Session, document_id: UUID) -> Document | None:
    return db.get(Document, document_id)


def list_documents(db: Session) -> list[Document]:
    return list(db.scalars(select(Document).order_by(Document.created_at.desc())))


def active_chunk_count(db: Session, document: Document) -> int:
    if document.active_run_id is None:
        return 0
    return int(db.scalar(select(func.count()).select_from(
        __import__('policy_api.models', fromlist=['DocumentChunk']).DocumentChunk
    ).where(
        __import__('policy_api.models', fromlist=['DocumentChunk']).DocumentChunk.ingestion_run_id == document.active_run_id
    )) or 0)


def list_active_chunks(db: Session, document: Document) -> list[DocumentChunk]:
    if document.active_run_id is None:
        return []
    return list(db.scalars(select(DocumentChunk).where(
        DocumentChunk.ingestion_run_id == document.active_run_id
    ).order_by(DocumentChunk.sequence)))


def list_ingestion_runs(db: Session, document_id: UUID) -> list[IngestionRun]:
    return list(db.scalars(select(IngestionRun).where(
        IngestionRun.document_id == document_id
    ).order_by(IngestionRun.created_at.desc(), IngestionRun.id.desc())))


def add_ingestion_run(db: Session, document: Document) -> IngestionRun:
    from policy_api.models import IngestionStage, IngestionStatus
    run = IngestionRun(document_id=document.id, stage=IngestionStage.VALIDATING, status=IngestionStatus.QUEUED)
    db.add(run)
    return run
