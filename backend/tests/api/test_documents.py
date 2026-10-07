from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete

from policy_api.auth.passwords import hash_password
from policy_api.config import Settings
from policy_api.database import create_database_engine, create_session_factory
from policy_api.documents.preview import PreviewResult
from policy_api.ingestion.worker import IngestionExecutor
from policy_api.ingestion.embedding_client import EmbeddingError
from policy_api.main import create_app
from policy_api.models import Answer, AnswerCitation, Document, DocumentChunk, DocumentStatus, Feedback, IngestionRun, IngestionStatus, Question, Session, User, UserRole


def settings(database_url: str, upload_root: Path) -> Settings:
    return Settings(app_env="test", database_url=database_url, session_secret="s" * 32,
        frontend_origins="http://localhost:5173", upload_root=upload_root, max_upload_bytes=1024,
        model_base_url="https://models.example.test/v1", model_api_key="key",
        chat_model="chat", embedding_model="embedding")


@pytest.fixture
def documents_app(TEST_DATABASE_URL: str, tmp_path: Path):  # type: ignore[invalid-name]
    engine = create_database_engine(TEST_DATABASE_URL)
    factory = create_session_factory(engine)
    with factory() as db:
        db.execute(delete(Feedback)); db.execute(delete(AnswerCitation)); db.execute(delete(Answer)); db.execute(delete(Question)); db.execute(delete(DocumentChunk)); db.execute(delete(IngestionRun)); db.execute(delete(Document)); db.execute(delete(Session)); db.execute(delete(User))
        db.add_all([
            User(username="employee", password_hash=hash_password("employee-password"), role=UserRole.EMPLOYEE),
            User(username="admin", password_hash=hash_password("admin-password"), role=UserRole.ADMIN),
        ]); db.commit()
    yield create_app(settings(TEST_DATABASE_URL, tmp_path), session_factory=factory), factory, tmp_path
    engine.dispose()


async def login(client: httpx.AsyncClient, username: str) -> None:
    response = await client.post("/api/v1/auth/login", json={"username": username, "password": f"{username}-password"})
    assert response.status_code == 204


@pytest.mark.anyio
async def test_admin_can_upload_list_disable_and_request_reindex(documents_app) -> None:
    app, factory, root = documents_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "admin")
        uploaded = await client.post("/api/v1/documents", files={"file": ("leave.txt", "休假制度".encode(), "text/plain")})
        listed = await client.get("/api/v1/documents")
        document_id = uploaded.json()["id"]
        disabled = await client.post(f"/api/v1/documents/{document_id}/disable")
        enabled = await client.post(f"/api/v1/documents/{document_id}/enable")
        reindex = await client.post(f"/api/v1/documents/{document_id}/reindex")

    assert uploaded.status_code == 201
    assert uploaded.json()["status"] == "pending"
    assert listed.json()[0]["display_name"] == "leave.txt"
    assert listed.json()[0]["updated_at"]
    assert listed.json()[0]["chunk_count"] == 0
    assert "error_code" in listed.json()[0]
    assert disabled.status_code == 204
    assert enabled.status_code == 409
    assert enabled.json()["code"] == "no_successful_index"
    assert reindex.status_code == 202
    assert len(list(root.glob("*.txt"))) == 1
    with factory() as db:
        assert db.query(IngestionRun).count() == 2


@pytest.mark.anyio
async def test_employee_cannot_upload_and_duplicate_hash_is_rejected(documents_app) -> None:
    app, _factory, root = documents_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as employee:
        await login(employee, "employee")
        forbidden = await employee.post("/api/v1/documents", files={"file": ("leave.txt", b"policy", "text/plain")})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin:
        await login(admin, "admin")
        first = await admin.post("/api/v1/documents", files={"file": ("one.txt", b"policy", "text/plain")})
        duplicate = await admin.post("/api/v1/documents", files={"file": ("two.txt", b"policy", "text/plain")})

    assert forbidden.status_code == 403
    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "duplicate_document"
    assert str(root) not in duplicate.text


@pytest.mark.anyio
async def test_admin_can_read_ordered_safe_ingestion_run_history(documents_app) -> None:
    app, _factory, _root = documents_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "admin")
        uploaded = await client.post("/api/v1/documents", files={"file": ("leave.txt", b"policy", "text/plain")})
        document_id = uploaded.json()["id"]
        await client.post(f"/api/v1/documents/{document_id}/reindex")
        runs = await client.get(f"/api/v1/documents/{document_id}/runs")

    assert runs.status_code == 200
    assert len(runs.json()) == 2
    assert [item["status"] for item in runs.json()] == ["queued", "queued"]
    assert set(runs.json()[0]) == {"id", "stage", "status", "error_code", "started_at", "finished_at", "created_at"}
    assert runs.json()[0]["created_at"] >= runs.json()[1]["created_at"]
    assert str(app.state.settings.upload_root) not in runs.text


@pytest.mark.anyio
async def test_employee_cannot_read_ingestion_run_history(documents_app) -> None:
    app, _factory, _root = documents_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin:
        await login(admin, "admin")
        uploaded = await admin.post("/api/v1/documents", files={"file": ("leave.txt", b"policy", "text/plain")})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as employee:
        await login(employee, "employee")
        response = await employee.get(f"/api/v1/documents/{uploaded.json()['id']}/runs")
    assert response.status_code == 403


@pytest.mark.anyio
async def test_admin_can_open_download_and_inspect_active_document_content(documents_app) -> None:
    app, factory, root = documents_app
    content = "第一章 休假制度\n\n员工每年享有五天年假。".encode()
    with factory() as db:
        validated = __import__('policy_api.documents.validation', fromlist=['validate_upload']).validate_upload(
            "leave.txt", "text/plain", content, max_bytes=1024)
        document = __import__('policy_api.documents.service', fromlist=['create_document']).create_document(
            db, root, "leave.txt", content, validated)
        document_id = document.id
    executor = IngestionExecutor(session_factory=factory, upload_root=root,
        embed=lambda texts: [[0.1, 0.2, 0.3] for _ in texts], batch_size=2, max_chars=100)
    assert executor.process_next() is True

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin:
        await login(admin, "admin")
        opened = await admin.get(f"/api/v1/documents/{document_id}/content")
        downloaded = await admin.get(f"/api/v1/documents/{document_id}/content?download=true")
        chunks = await admin.get(f"/api/v1/documents/{document_id}/chunks")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as employee:
        await login(employee, "employee")
        forbidden_content = await employee.get(f"/api/v1/documents/{document_id}/content")
        forbidden_chunks = await employee.get(f"/api/v1/documents/{document_id}/chunks")

    assert opened.status_code == 200 and opened.content == content
    assert opened.headers["content-type"].startswith("text/plain")
    assert opened.headers["content-disposition"].startswith("inline;")
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert chunks.status_code == 200
    assert [item["sequence"] for item in chunks.json()] == [0, 1]
    assert [item["location"] for item in chunks.json()] == ["paragraph:1", "paragraph:2"]
    assert [item["text"] for item in chunks.json()] == ["第一章 休假制度", "员工每年享有五天年假。"]
    assert all(item["heading_path"] is None and item["page_number"] is None for item in chunks.json())
    assert forbidden_content.status_code == forbidden_chunks.status_code == 403
    assert str(root) not in opened.text + downloaded.text + chunks.text


@pytest.mark.anyio
async def test_missing_or_unsafe_document_content_returns_safe_404(documents_app) -> None:
    app, factory, root = documents_app
    with factory() as db:
        document = Document(display_name="missing.pdf", storage_key="../outside.pdf", sha256="e" * 64,
            mime_type="application/pdf", status=DocumentStatus.DISABLED, is_enabled=False)
        db.add(document); db.commit(); document_id = document.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "admin")
        unsafe = await client.get(f"/api/v1/documents/{document_id}/content")
        missing = await client.get(f"/api/v1/documents/{__import__('uuid').uuid4()}/content")
    assert unsafe.status_code == missing.status_code == 404
    assert unsafe.json()["code"] == missing.json()["code"] == "document_not_found"
    assert str(root) not in unsafe.text + missing.text


def test_executor_processes_uploaded_text_to_an_enabled_atomic_index(documents_app) -> None:
    _app, factory, root = documents_app
    with factory() as db:
        validated = __import__('policy_api.documents.validation', fromlist=['validate_upload']).validate_upload(
            "leave.txt", "text/plain", "第一条 年假五天。\n第二条 需要审批。".encode(), max_bytes=1024
        )
        document = __import__('policy_api.documents.service', fromlist=['create_document']).create_document(
            db, root, "leave.txt", "第一条 年假五天。\n第二条 需要审批。".encode(), validated
        )
        document_id = document.id

    executor = IngestionExecutor(
        session_factory=factory,
        upload_root=root,
        embed=lambda texts: [[0.1, 0.2, 0.3] for _ in texts],
        batch_size=2,
        max_chars=100,
    )
    assert executor.process_next() is True
    assert executor.process_next() is False

    with factory() as db:
        document = db.get(Document, document_id)
        run = db.query(IngestionRun).filter_by(document_id=document_id).one()
        chunks = db.query(DocumentChunk).filter_by(document_id=document_id).all()
        assert document and document.status == DocumentStatus.ENABLED and document.is_enabled
        assert document.active_run_id == run.id
        assert run.status == IngestionStatus.COMPLETED
        assert chunks and "年假五天" in "".join(chunk.text for chunk in chunks)



@pytest.mark.anyio
async def test_document_list_counts_only_active_index_chunks(documents_app) -> None:
    app, factory, _root = documents_app
    with factory() as db:
        document = Document(display_name="policy.txt", storage_key="count-policy.txt", sha256="c" * 64,
            mime_type="text/plain", status=DocumentStatus.ENABLED, is_enabled=True)
        db.add(document); db.flush()
        active = IngestionRun(document_id=document.id,
            stage=__import__('policy_api.models', fromlist=['IngestionStage']).IngestionStage.COMPLETED,
            status=IngestionStatus.COMPLETED)
        historical = IngestionRun(document_id=document.id,
            stage=__import__('policy_api.models', fromlist=['IngestionStage']).IngestionStage.COMPLETED,
            status=IngestionStatus.COMPLETED)
        db.add_all([active, historical]); db.flush(); document.active_run_id = active.id
        db.add_all([
            DocumentChunk(ingestion_run_id=active.id, document_id=document.id, sequence=index,
                text=f"active {index}", text_hash=f"{index + 1:064x}", embedding=[0.0, 0.0, 0.0])
            for index in range(2)
        ] + [DocumentChunk(ingestion_run_id=historical.id, document_id=document.id, sequence=0,
            text="historical", text_hash="f" * 64, embedding=[0.0, 0.0, 0.0])])
        db.commit(); document_id = str(document.id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "admin")
        rows = (await client.get("/api/v1/documents")).json()

    row = next(item for item in rows if item["id"] == document_id)
    assert row["chunk_count"] == 2


def test_executor_maps_parser_failure_without_exposing_storage_path(documents_app) -> None:
    _app, factory, root = documents_app
    with factory() as db:
        document = Document(display_name="broken.pdf", storage_key="broken.pdf", sha256="a" * 64,
            mime_type="application/pdf", status=DocumentStatus.PENDING, is_enabled=False)
        db.add(document); db.flush()
        db.add(IngestionRun(document_id=document.id,
            stage=__import__('policy_api.models', fromlist=['IngestionStage']).IngestionStage.VALIDATING,
            status=IngestionStatus.QUEUED)); db.commit(); document_id = document.id
    (root / "broken.pdf").write_bytes(b"not a pdf")

    executor = IngestionExecutor(session_factory=factory, upload_root=root,
        embed=lambda texts: [[0.1, 0.2, 0.3] for _ in texts], batch_size=2, max_chars=100)
    assert executor.process_next() is True

    with factory() as db:
        document = db.get(Document, document_id)
        run = db.query(IngestionRun).filter_by(document_id=document_id).one()
        assert document and document.status == DocumentStatus.PARSE_FAILED
        assert document.error_code == "pdf_invalid"
        assert run.status == IngestionStatus.FAILED
        assert str(root) not in (run.error_code or "")


def test_executor_maps_embedding_failure_to_index_failed(documents_app) -> None:
    _app, factory, root = documents_app
    content = b"policy text"
    with factory() as db:
        validated = __import__('policy_api.documents.validation', fromlist=['validate_upload']).validate_upload(
            "policy.txt", "text/plain", content, max_bytes=1024)
        document = __import__('policy_api.documents.service', fromlist=['create_document']).create_document(
            db, root, "policy.txt", content, validated)
        document_id = document.id

    def unavailable(_texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("embedding_request_failed")

    assert IngestionExecutor(session_factory=factory, upload_root=root, embed=unavailable,
        batch_size=2, max_chars=100).process_next() is True
    with factory() as db:
        document = db.get(Document, document_id)
        assert document and document.status == DocumentStatus.INDEX_FAILED
        assert not document.is_enabled
        assert document.error_code == "embedding_request_failed"


def test_executor_treats_embedding_shape_mismatch_as_index_failure(documents_app) -> None:
    _app, factory, root = documents_app
    content = b"policy text"
    with factory() as db:
        validated = __import__('policy_api.documents.validation', fromlist=['validate_upload']).validate_upload(
            "policy.txt", "text/plain", content, max_bytes=1024)
        document = __import__('policy_api.documents.service', fromlist=['create_document']).create_document(
            db, root, "policy.txt", content, validated)
        document_id = document.id

    assert IngestionExecutor(session_factory=factory, upload_root=root, embed=lambda _texts: [],
        batch_size=2, max_chars=100).process_next() is True
    with factory() as db:
        document = db.get(Document, document_id)
        assert document and document.status == DocumentStatus.INDEX_FAILED
        assert document.error_code == "embedding_count_mismatch"


def test_failed_reindex_preserves_last_active_index(documents_app) -> None:
    _app, factory, root = documents_app
    content = b"policy text"
    with factory() as db:
        validated = __import__('policy_api.documents.validation', fromlist=['validate_upload']).validate_upload(
            "policy.txt", "text/plain", content, max_bytes=1024)
        document = __import__('policy_api.documents.service', fromlist=['create_document']).create_document(
            db, root, "policy.txt", content, validated)
        document_id = document.id
    success = IngestionExecutor(session_factory=factory, upload_root=root,
        embed=lambda texts: [[0.1, 0.2, 0.3] for _ in texts], batch_size=2, max_chars=100)
    assert success.process_next() is True
    with factory() as db:
        active_run_id = db.get(Document, document_id).active_run_id
        __import__('policy_api.documents.service', fromlist=['request_reindex']).request_reindex(db, document_id)

    def unavailable(_texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("embedding_timeout")

    assert IngestionExecutor(session_factory=factory, upload_root=root, embed=unavailable,
        batch_size=2, max_chars=100).process_next() is True
    with factory() as db:
        document = db.get(Document, document_id)
        failed = db.query(IngestionRun).filter_by(document_id=document_id,
            status=IngestionStatus.FAILED).one()
        assert document and document.active_run_id == active_run_id
        assert document.status == DocumentStatus.ENABLED and document.is_enabled
        assert document.error_code == "embedding_timeout"
        assert failed.error_code == "embedding_timeout"


@pytest.mark.anyio
async def test_admin_previews_pdf_and_converted_docx_but_employee_cannot(documents_app) -> None:
    app, factory, root = documents_app
    pdf_bytes = b"%PDF-1.7\noriginal"; docx_bytes = b"PK\x03\x04docx"
    (root / "policy.pdf").write_bytes(pdf_bytes); (root / "policy.docx").write_bytes(docx_bytes)
    converted = root / "converted.pdf"; converted.write_bytes(b"%PDF-1.7\nconverted")
    with factory() as db:
        pdf = Document(display_name="policy.pdf", storage_key="policy.pdf", sha256="4" * 64,
            mime_type="application/pdf", status=DocumentStatus.ENABLED, is_enabled=True)
        docx = Document(display_name="policy.docx", storage_key="policy.docx", sha256="5" * 64,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            status=DocumentStatus.ENABLED, is_enabled=True)
        db.add_all([pdf, docx]); db.commit(); pdf_id, docx_id = pdf.id, docx.id

    calls: list[str] = []
    def preview_builder(source, _root, _document_id, _sha256, mime_type):  # type: ignore[no-untyped-def]
        calls.append(mime_type)
        if mime_type == "application/pdf": return PreviewResult(source, "application/pdf", source.name)
        return PreviewResult(converted, "application/pdf", "policy-preview.pdf")
    app.state.preview_builder = preview_builder

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin:
        await login(admin, "admin")
        pdf_response = await admin.get(f"/api/v1/documents/{pdf_id}/preview")
        docx_response = await admin.get(f"/api/v1/documents/{docx_id}/preview")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as employee:
        await login(employee, "employee"); forbidden = await employee.get(f"/api/v1/documents/{docx_id}/preview")

    assert pdf_response.status_code == docx_response.status_code == 200
    assert pdf_response.content == pdf_bytes and docx_response.content.endswith(b"converted")
    assert pdf_response.headers["content-type"].startswith("application/pdf")
    assert docx_response.headers["content-disposition"].startswith("inline;")
    assert docx_response.headers["x-content-type-options"] == "nosniff"
    assert docx_response.headers["cache-control"] == "private, no-store"
    assert forbidden.status_code == 403
    assert calls == ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
    assert str(root) not in pdf_response.text + docx_response.text + forbidden.text


@pytest.mark.anyio
async def test_preview_returns_safe_errors_for_unsupported_missing_and_conversion_failure(documents_app) -> None:
    app, factory, root = documents_app
    (root / "policy.txt").write_text("policy", encoding="utf-8")
    (root / "broken.docx").write_bytes(b"PK\x03\x04docx")
    with factory() as db:
        text = Document(display_name="policy.txt", storage_key="policy.txt", sha256="6" * 64,
            mime_type="text/plain", status=DocumentStatus.ENABLED, is_enabled=True)
        broken = Document(display_name="broken.docx", storage_key="broken.docx", sha256="7" * 64,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            status=DocumentStatus.ENABLED, is_enabled=True)
        unsafe_doc = Document(display_name="unsafe.docx", storage_key="../unsafe.docx", sha256="8" * 64,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            status=DocumentStatus.ENABLED, is_enabled=True)
        db.add_all([text, broken, unsafe_doc]); db.commit(); ids = text.id, broken.id, unsafe_doc.id

    from policy_api.documents.preview import PreviewError
    def preview_builder(_source, _root, _document_id, _sha256, mime_type):  # type: ignore[no-untyped-def]
        raise PreviewError("preview_not_supported" if mime_type == "text/plain" else "preview_timeout")
    app.state.preview_builder = preview_builder

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin:
        await login(admin, "admin")
        unsupported = await admin.get(f"/api/v1/documents/{ids[0]}/preview")
        failed = await admin.get(f"/api/v1/documents/{ids[1]}/preview")
        unsafe = await admin.get(f"/api/v1/documents/{ids[2]}/preview")
        missing = await admin.get(f"/api/v1/documents/{__import__('uuid').uuid4()}/preview")

    assert unsupported.status_code == 422 and unsupported.json()["code"] == "preview_not_supported"
    assert failed.status_code == 503 and failed.json()["code"] == "preview_timeout"
    assert unsafe.status_code == missing.status_code == 404
    assert unsafe.json()["code"] == missing.json()["code"] == "document_not_found"
    assert str(root) not in unsupported.text + failed.text + unsafe.text + missing.text
