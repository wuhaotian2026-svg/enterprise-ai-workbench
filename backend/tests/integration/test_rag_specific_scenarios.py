from __future__ import annotations

import json
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, text

from policy_api.database import (
    assert_test_database_url,
    create_database_engine,
    create_session_factory,
)
from policy_api.models import (
    Document,
    DocumentChunk,
    DocumentStatus,
    IngestionRun,
    IngestionStage,
    IngestionStatus,
)
from policy_api.retrieval.repository import SqlAlchemyRetrievalRepository


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


def migrate() -> None:
    config = Config("backend/alembic.ini")
    config.set_main_option("script_location", "backend/alembic")
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    try:
        command.upgrade(config, "head")
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original


def add_document(
    database,
    *,
    name: str,
    document_status: DocumentStatus,
    enabled: bool,
    chunk_text: str,
) -> Document:
    identifier = uuid.uuid4().hex
    document = Document(
        display_name=name,
        storage_key=f"{identifier}.txt",
        sha256=identifier.ljust(64, "0"),
        mime_type="text/plain",
        status=document_status,
        is_enabled=enabled,
    )
    database.add(document)
    database.flush()
    run = IngestionRun(
        document_id=document.id,
        stage=IngestionStage.COMPLETED,
        status=IngestionStatus.COMPLETED,
        parameters={},
    )
    database.add(run)
    database.flush()
    document.active_run_id = run.id
    database.add(
        DocumentChunk(
            ingestion_run_id=run.id,
            document_id=document.id,
            sequence=0,
            text=chunk_text,
            text_hash=uuid.uuid4().hex.ljust(64, "0"),
            embedding=[0.1, 0.2, 0.3],
        )
    )
    return document


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is required")
def test_chinese_trigram_retrieval_uses_index_and_excludes_disabled_sources() -> None:
    assert_test_database_url(TEST_DATABASE_URL)
    migrate()
    engine = create_database_engine(TEST_DATABASE_URL)
    factory = create_session_factory(engine)
    with factory() as database:
        database.execute(delete(DocumentChunk))
        database.execute(delete(IngestionRun))
        database.execute(delete(Document))
        official = add_document(
            database,
            name="差旅费用管理制度.txt",
            document_status=DocumentStatus.ENABLED,
            enabled=True,
            chunk_text=(
                "出差住宿和餐补标准：一线城市住宿500元每晚，"
                "其他城市住宿350元每晚，餐补100元每天。"
            ),
        )
        add_document(
            database,
            name="火星出差演示.txt",
            document_status=DocumentStatus.DISABLED,
            enabled=False,
            chunk_text="南京出差三天住宿餐补，火星基地每天补贴999元。",
        )
        database.commit()

        repository = SqlAlchemyRetrievalRepository(database)
        results = repository.lexical_search("南京出差三天住宿餐补", limit=8)
        assert any(item.document_id == official.id for item in results)
        assert all(item.document_name != "火星出差演示.txt" for item in results)
        assert all(item.lexical_score is not None for item in results)
        assert [item.chunk_id for item in results] == [
            item.chunk_id
            for item in sorted(
                results,
                key=lambda item: (-float(item.lexical_score or 0), str(item.chunk_id)),
            )
        ]

        database.execute(text("SET LOCAL enable_seqscan = off"))
        plan = database.execute(
            text(
                "EXPLAIN (FORMAT JSON) "
                "SELECT id FROM document_chunks "
                "WHERE text % '出差住宿餐补标准'"
            )
        ).scalar_one()
        assert "ix_document_chunks_text_trgm" in json.dumps(plan)
    engine.dispose()
