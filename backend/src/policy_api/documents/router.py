from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session as DatabaseSession

from policy_api.auth.router import current_identity, database_session, error
from policy_api.documents.repository import active_chunk_count, get_document, list_active_chunks, list_documents, list_ingestion_runs
from policy_api.documents.preview import PreviewError, build_preview
from policy_api.documents.schemas import DocumentChunkResponse, DocumentResponse, IngestionRunResponse
from policy_api.documents.service import DocumentConflict, DocumentNotFound, create_document, disable_document, enable_document, request_reindex
from policy_api.documents.validation import UploadValidationError, validate_upload
from policy_api.models import Session, User, UserRole


router = APIRouter(prefix="/documents", tags=["documents"])


def require_admin(request: Request, identity: tuple[User, Session] = Depends(current_identity)) -> tuple[User, Session] | JSONResponse:
    if identity[0].role != UserRole.ADMIN:
        return error(request, 403, "admin_required", "Administrator access is required.")
    return identity


def document_response(document, *, chunk_count: int = 0) -> DocumentResponse:
    return DocumentResponse(id=document.id, display_name=document.display_name, mime_type=document.mime_type,
        status=document.status.value, is_enabled=document.is_enabled, error_code=document.error_code,
        updated_at=document.updated_at, chunk_count=chunk_count)


@router.post("", status_code=201, response_model=DocumentResponse)
async def upload(request: Request, file: UploadFile = File(), _admin=Depends(require_admin), db: DatabaseSession = Depends(database_session)):
    if isinstance(_admin, JSONResponse): return _admin
    limit = request.app.state.settings.max_upload_bytes
    content = await file.read(limit + 1)
    try:
        validated = validate_upload(file.filename or "", file.content_type or "", content, max_bytes=limit)
        return document_response(create_document(db, request.app.state.settings.upload_root, file.filename or "", content, validated))
    except UploadValidationError as exc:
        return error(request, 422, str(exc), "The uploaded file is invalid.")
    except DocumentConflict as exc:
        return error(request, 409, str(exc), "The document conflicts with existing state.")


@router.get("", response_model=list[DocumentResponse])
def list_all(request: Request, _admin=Depends(require_admin), db: DatabaseSession = Depends(database_session)):
    if isinstance(_admin, JSONResponse): return _admin
    return [document_response(item, chunk_count=active_chunk_count(db, item)) for item in list_documents(db)]


@router.get("/{document_id}/runs", response_model=list[IngestionRunResponse])
def runs(request: Request, document_id: UUID, _admin=Depends(require_admin), db: DatabaseSession = Depends(database_session)):
    if isinstance(_admin, JSONResponse): return _admin
    if get_document(db, document_id) is None:
        return error(request, 404, "document_not_found", "The document was not found.")
    return [IngestionRunResponse(id=item.id, stage=item.stage.value, status=item.status.value,
        error_code=item.error_code, started_at=item.started_at, finished_at=item.finished_at,
        created_at=item.created_at) for item in list_ingestion_runs(db, document_id)]


@router.get("/{document_id}/content", response_model=None)
def content(request: Request, document_id: UUID, download: bool = Query(False),
            _admin=Depends(require_admin), db: DatabaseSession = Depends(database_session)):
    if isinstance(_admin, JSONResponse): return _admin
    document = get_document(db, document_id)
    if document is None:
        return error(request, 404, "document_not_found", "The document was not found.")
    root = request.app.state.settings.upload_root.resolve()
    path = (root / document.storage_key).resolve()
    if path.parent != root or not path.is_file():
        return error(request, 404, "document_not_found", "The document was not found.")
    return FileResponse(path, media_type=document.mime_type, filename=document.display_name,
        content_disposition_type="attachment" if download else "inline",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.get("/{document_id}/preview", response_model=None)
def preview(request: Request, document_id: UUID, _admin=Depends(require_admin),
            db: DatabaseSession = Depends(database_session)):
    if isinstance(_admin, JSONResponse): return _admin
    document = get_document(db, document_id)
    if document is None:
        return error(request, 404, "document_not_found", "The document was not found.")
    root = request.app.state.settings.upload_root.resolve()
    source = (root / document.storage_key).resolve()
    if source.parent != root or not source.is_file():
        return error(request, 404, "document_not_found", "The document was not found.")
    builder = getattr(request.app.state, "preview_builder", build_preview)
    try:
        result = builder(source, root, document.id, document.sha256, document.mime_type)
    except PreviewError as exc:
        code = str(exc)
        status = 422 if code == "preview_not_supported" else 503
        if code == "document_not_found": status = 404
        if code not in {"preview_not_supported", "preview_timeout", "preview_failed", "document_not_found"}:
            code, status = "preview_failed", 503
        return error(request, status, code, "The document preview is unavailable.")
    return FileResponse(result.path, media_type=result.media_type, filename=result.display_name,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.get("/{document_id}/chunks", response_model=list[DocumentChunkResponse])
def chunks(request: Request, document_id: UUID, _admin=Depends(require_admin),
           db: DatabaseSession = Depends(database_session)):
    if isinstance(_admin, JSONResponse): return _admin
    document = get_document(db, document_id)
    if document is None:
        return error(request, 404, "document_not_found", "The document was not found.")
    return [DocumentChunkResponse(id=item.id, sequence=item.sequence, heading_path=item.heading_path,
        page_number=item.page_number, location=item.location, text=item.text)
        for item in list_active_chunks(db, document)]


def mutate(request: Request, action, document_id: UUID, db: DatabaseSession):
    try: action(db, document_id)
    except DocumentNotFound as exc: return error(request, 404, str(exc), "The document was not found.")
    except DocumentConflict as exc: return error(request, 409, str(exc), "The document state does not allow this action.")
    return Response(status_code=204)


@router.post("/{document_id}/disable", response_model=None)
def disable(request: Request, document_id: UUID, _admin=Depends(require_admin), db: DatabaseSession = Depends(database_session)):
    if isinstance(_admin, JSONResponse): return _admin
    return mutate(request, disable_document, document_id, db)


@router.post("/{document_id}/enable", response_model=None)
def enable(request: Request, document_id: UUID, _admin=Depends(require_admin), db: DatabaseSession = Depends(database_session)):
    if isinstance(_admin, JSONResponse): return _admin
    return mutate(request, enable_document, document_id, db)


@router.post("/{document_id}/reindex", response_model=None)
def reindex(request: Request, document_id: UUID, _admin=Depends(require_admin), db: DatabaseSession = Depends(database_session)):
    if isinstance(_admin, JSONResponse): return _admin
    result = mutate(request, request_reindex, document_id, db)
    return Response(status_code=202) if result.status_code == 204 else result
