from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select

from policy_api.database import assert_test_database_url, create_database_engine, create_session_factory
from policy_api.ingestion.indexer import PreparedChunk, persist_index_atomically
from policy_api.models import Document, DocumentChunk, DocumentStatus, IngestionRun, IngestionStage, IngestionStatus


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


def prepared(sequence: int, text: str) -> PreparedChunk:
    return PreparedChunk(sequence=sequence, text=text, text_hash=f"{sequence + 1:064x}",
        embedding=(0.1, 0.2, 0.3), page=1, heading_path=("制度",), location=f"page:1:{sequence}")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is required")
def test_atomic_index_switch_rolls_back_failed_batch_and_switches_only_after_success() -> None:
    assert_test_database_url(TEST_DATABASE_URL)
    config = Config("backend/alembic.ini")
    config.set_main_option("script_location", "backend/alembic")
    original_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    try:
        command.upgrade(config, "head")
    finally:
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url
    engine = create_database_engine(TEST_DATABASE_URL); factory = create_session_factory(engine)
    with factory() as db:
        db.execute(delete(DocumentChunk)); db.execute(delete(IngestionRun)); db.execute(delete(Document)); db.commit()
        document = Document(display_name="policy.txt", storage_key=f"{uuid.uuid4()}.txt",
            sha256=uuid.uuid4().hex.ljust(64, "0"), mime_type="text/plain",
            status=DocumentStatus.ENABLED, is_enabled=True)
        db.add(document); db.flush()
        old_run = IngestionRun(document_id=document.id, stage=IngestionStage.COMPLETED,
            status=IngestionStatus.COMPLETED, parameters={})
        db.add(old_run); db.flush()
        db.add(DocumentChunk(ingestion_run_id=old_run.id, document_id=document.id, sequence=0,
            text="旧制度", text_hash="f" * 64, embedding=[0.0, 0.0, 0.0]))
        document.active_run_id = old_run.id
        failed_run = IngestionRun(document_id=document.id, stage=IngestionStage.PERSISTING,
            status=IngestionStatus.RUNNING, parameters={})
        db.add(failed_run); db.commit()
        document_id, old_run_id, failed_run_id = document.id, old_run.id, failed_run.id

    with factory() as db:
        with pytest.raises(Exception):
            persist_index_atomically(db, document_id, failed_run_id,
                [prepared(0, "新制度一"), prepared(0, "重复序号")])

    with factory() as db:
        document = db.get(Document, document_id); failed = db.get(IngestionRun, failed_run_id)
        assert document and document.active_run_id == old_run_id and document.is_enabled
        assert failed and failed.status == IngestionStatus.FAILED
        assert db.scalar(select(DocumentChunk).where(DocumentChunk.ingestion_run_id == failed_run_id)) is None
        success = IngestionRun(document_id=document_id, stage=IngestionStage.PERSISTING,
            status=IngestionStatus.RUNNING, parameters={})
        db.add(success); db.commit(); success_id = success.id

    with factory() as db:
        persist_index_atomically(db, document_id, success_id, [prepared(0, "新制度一"), prepared(1, "新制度二")])

    with factory() as db:
        document = db.get(Document, document_id); success = db.get(IngestionRun, success_id)
        chunks = list(db.scalars(select(DocumentChunk).where(DocumentChunk.ingestion_run_id == success_id).order_by(DocumentChunk.sequence)))
        assert document and document.active_run_id == success_id and document.status == DocumentStatus.ENABLED
        assert success and success.status == IngestionStatus.COMPLETED and success.stage == IngestionStage.COMPLETED
        assert [chunk.text for chunk in chunks] == ["新制度一", "新制度二"]
    engine.dispose()
