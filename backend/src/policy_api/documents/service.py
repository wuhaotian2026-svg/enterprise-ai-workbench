from __future__ import annotations

from pathlib import Path
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from policy_api.documents.repository import add_ingestion_run, find_by_hash, get_document
from policy_api.documents.storage import store_bytes
from policy_api.documents.validation import ValidatedUpload
from policy_api.models import Document, DocumentStatus, IngestionStatus


class DocumentConflict(ValueError): pass
class DocumentNotFound(ValueError): pass


def create_document(db: Session, root: Path, filename: str, content: bytes, validated: ValidatedUpload) -> Document:
    if find_by_hash(db, validated.sha256):
        raise DocumentConflict("duplicate_document")
    stored = store_bytes(root, validated.extension, content)
    document = Document(display_name=filename, storage_key=stored.key, sha256=validated.sha256,
        mime_type=validated.mime_type, status=DocumentStatus.PENDING, is_enabled=False)
    db.add(document)
    try:
        db.flush(); add_ingestion_run(db, document); db.commit(); db.refresh(document)
    except IntegrityError:
        db.rollback(); stored.path.unlink(missing_ok=True); raise DocumentConflict("duplicate_document")
    return document


def disable_document(db: Session, document_id: UUID) -> None:
    document = get_document(db, document_id)
    if not document: raise DocumentNotFound("document_not_found")
    document.is_enabled = False; document.status = DocumentStatus.DISABLED; db.commit()


def enable_document(db: Session, document_id: UUID) -> None:
    document = get_document(db, document_id)
    if not document: raise DocumentNotFound("document_not_found")
    completed = any(run.status == IngestionStatus.COMPLETED for run in db.query(__import__('policy_api.models', fromlist=['IngestionRun']).IngestionRun).filter_by(document_id=document.id))
    if not completed: raise DocumentConflict("no_successful_index")
    document.is_enabled = True; document.status = DocumentStatus.ENABLED; db.commit()


def request_reindex(db: Session, document_id: UUID) -> None:
    document = get_document(db, document_id)
    if not document: raise DocumentNotFound("document_not_found")
    add_ingestion_run(db, document); db.commit()
