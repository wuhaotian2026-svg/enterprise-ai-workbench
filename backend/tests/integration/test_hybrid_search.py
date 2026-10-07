from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, or_, select

from policy_api.database import assert_test_database_url, create_database_engine, create_session_factory
from policy_api.models import Document, DocumentChunk, DocumentStatus, IngestionRun, IngestionStage, IngestionStatus
from policy_api.retrieval.repository import SqlAlchemyRetrievalRepository, _lexical_probes
from policy_api.retrieval.service import RetrievalService


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


def migrate() -> None:
    config = Config("backend/alembic.ini"); config.set_main_option("script_location", "backend/alembic")
    original = os.environ.get("DATABASE_URL"); os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    try: command.upgrade(config, "head")
    finally:
        if original is None: os.environ.pop("DATABASE_URL", None)
        else: os.environ["DATABASE_URL"] = original


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is required")
def test_hybrid_search_excludes_disabled_inactive_and_blank_chunks() -> None:
    assert_test_database_url(TEST_DATABASE_URL); migrate()
    engine = create_database_engine(TEST_DATABASE_URL); factory = create_session_factory(engine)
    with factory() as db:
        db.execute(delete(DocumentChunk)); db.execute(delete(IngestionRun)); db.execute(delete(Document)); db.commit()
        enabled = Document(display_name="Leave Policy.txt", storage_key=f"{uuid.uuid4()}.txt", sha256=uuid.uuid4().hex.ljust(64,"0"),
            mime_type="text/plain", status=DocumentStatus.ENABLED, is_enabled=True)
        disabled = Document(display_name="Disabled.txt", storage_key=f"{uuid.uuid4()}.txt", sha256=uuid.uuid4().hex.ljust(64,"0"),
            mime_type="text/plain", status=DocumentStatus.DISABLED, is_enabled=False)
        db.add_all([enabled, disabled]); db.flush()
        active = IngestionRun(document_id=enabled.id, stage=IngestionStage.COMPLETED, status=IngestionStatus.COMPLETED, parameters={})
        old = IngestionRun(document_id=enabled.id, stage=IngestionStage.COMPLETED, status=IngestionStatus.COMPLETED, parameters={})
        disabled_run = IngestionRun(document_id=disabled.id, stage=IngestionStage.COMPLETED, status=IngestionStatus.COMPLETED, parameters={})
        db.add_all([active, old, disabled_run]); db.flush(); enabled.active_run_id=active.id; disabled.active_run_id=disabled_run.id
        db.add_all([
            DocumentChunk(ingestion_run_id=active.id, document_id=enabled.id, sequence=0, heading_path="Leave > Annual", page_number=3,
                location="page:3", text="annual leave requires manager approval", text_hash="1"*64, embedding=[0.0,0.0,0.0]),
            DocumentChunk(ingestion_run_id=active.id, document_id=enabled.id, sequence=1, text="expense reimbursement rules", text_hash="2"*64, embedding=[1.0,0.0,0.0]),
            DocumentChunk(ingestion_run_id=active.id, document_id=enabled.id, sequence=2, text="   ", text_hash="3"*64, embedding=[1.0,0.0,0.0]),
            DocumentChunk(ingestion_run_id=old.id, document_id=enabled.id, sequence=0, text="annual leave old rule", text_hash="4"*64, embedding=[1.0,0.0,0.0]),
            DocumentChunk(ingestion_run_id=disabled_run.id, document_id=disabled.id, sequence=0, text="annual leave disabled rule", text_hash="5"*64, embedding=[1.0,0.0,0.0]),
        ]); db.commit()

    with factory() as db:
        repository = SqlAlchemyRetrievalRepository(db)
        keyword = repository.lexical_search("annual leave", 10)
        batched_keyword = repository.lexical_search_many(
            ("annual leave", "expense reimbursement"),
            10,
        )
        vector = repository.vector_search([1.0,0.0,0.0], 10)
        result = RetrievalService(repository=repository, embed=lambda _texts:[[1.0,0.0,0.0]]).retrieve("annual leave", top_k=5)
        assert [item.text for item in keyword] == ["annual leave requires manager approval"]
        assert [item.text for item in batched_keyword[0]] == [item.text for item in keyword]
        assert [item.text for item in batched_keyword[1]] == ["expense reimbursement rules"]
        assert all(
            item.document_name != "Disabled.txt"
            for ranking in batched_keyword
            for item in ranking
        )
        assert keyword[0].lexical_score is not None
        assert keyword[0].vector_score is None
        assert [item.text for item in vector] == ["expense reimbursement rules", "annual leave requires manager approval"]
        assert all(item.vector_score is not None for item in vector)
        assert all(item.lexical_score is None for item in vector)
        assert {item.text for item in result} == {"annual leave requires manager approval", "expense reimbursement rules"}
        annual = next(item for item in result if item.text.startswith("annual"))
        assert annual.document_name == "Leave Policy.txt" and annual.page == 3
        assert annual.heading_path == "Leave > Annual" and annual.location == "page:3"
    engine.dispose()


def _legacy_lexical_rows(db, query: str, limit: int):
    probes = _lexical_probes(query)
    scores = tuple(
        expression
        for probe in probes
        for expression in (
            func.similarity(DocumentChunk.text, probe),
            func.word_similarity(probe, DocumentChunk.text),
        )
    )
    score = func.greatest(*scores)
    predicates = tuple(
        predicate
        for probe in probes
        for predicate in (
            DocumentChunk.text.op("%")(probe),
            DocumentChunk.text.op("%>")(probe),
        )
    )
    statement = (
        select(
            DocumentChunk.id.label("chunk_id"),
            DocumentChunk.text.label("text"),
            score.label("lexical_score"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            Document.is_enabled.is_(True),
            Document.status == DocumentStatus.ENABLED,
            Document.active_run_id == DocumentChunk.ingestion_run_id,
            func.length(func.btrim(DocumentChunk.text)) > 0,
            or_(*predicates),
        )
        .order_by(score.desc(), DocumentChunk.id)
        .limit(limit)
    )
    with db.begin_nested():
        db.execute(
            select(func.set_config("pg_trgm.similarity_threshold", "0.10", True))
        )
        db.execute(
            select(
                func.set_config("pg_trgm.word_similarity_threshold", "0.20", True)
            )
        )
        return db.execute(statement).all()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is required")
def test_fixed_probe_slots_match_legacy_results_for_short_and_long_queries() -> None:
    assert_test_database_url(TEST_DATABASE_URL)
    migrate()
    engine = create_database_engine(TEST_DATABASE_URL)
    factory = create_session_factory(engine)
    with factory() as db:
        db.execute(delete(DocumentChunk))
        db.execute(delete(IngestionRun))
        db.execute(delete(Document))
        db.commit()

        enabled = Document(
            display_name="Annual Leave.txt",
            storage_key=f"{uuid.uuid4()}.txt",
            sha256=uuid.uuid4().hex.ljust(64, "0"),
            mime_type="text/plain",
            status=DocumentStatus.ENABLED,
            is_enabled=True,
        )
        disabled = Document(
            display_name="Disabled Annual Leave.txt",
            storage_key=f"{uuid.uuid4()}.txt",
            sha256=uuid.uuid4().hex.ljust(64, "0"),
            mime_type="text/plain",
            status=DocumentStatus.DISABLED,
            is_enabled=False,
        )
        db.add_all([enabled, disabled])
        db.flush()
        active = IngestionRun(
            document_id=enabled.id,
            stage=IngestionStage.COMPLETED,
            status=IngestionStatus.COMPLETED,
            parameters={},
        )
        disabled_run = IngestionRun(
            document_id=disabled.id,
            stage=IngestionStage.COMPLETED,
            status=IngestionStatus.COMPLETED,
            parameters={},
        )
        db.add_all([active, disabled_run])
        db.flush()
        enabled.active_run_id = active.id
        disabled.active_run_id = disabled_run.id
        db.add_all(
            [
                DocumentChunk(
                    ingestion_run_id=active.id,
                    document_id=enabled.id,
                    sequence=0,
                    text="annual leave requires manager approval",
                    text_hash="a" * 64,
                    embedding=[0.0, 0.0, 0.0],
                ),
                DocumentChunk(
                    ingestion_run_id=active.id,
                    document_id=enabled.id,
                    sequence=1,
                    text="annual leave policy requires director approval",
                    text_hash="b" * 64,
                    embedding=[0.0, 0.0, 0.0],
                ),
                DocumentChunk(
                    ingestion_run_id=disabled_run.id,
                    document_id=disabled.id,
                    sequence=0,
                    text="annual leave policy manager approval requirements",
                    text_hash="c" * 64,
                    embedding=[0.0, 0.0, 0.0],
                ),
            ]
        )
        db.commit()

    short_query = "annual leave"
    long_query = "please explain annual leave policy manager approval requirements"
    assert len(_lexical_probes(short_query)) < len(_lexical_probes(long_query))

    with factory() as db:
        repository = SqlAlchemyRetrievalRepository(db)
        for query in (short_query, long_query):
            expected = _legacy_lexical_rows(db, query, 10)
            actual = repository.lexical_search(query, 10)

            assert actual
            assert [item.chunk_id for item in actual] == [row.chunk_id for row in expected]
            assert [item.text for item in actual] == [row.text for row in expected]
            assert [item.lexical_score for item in actual] == pytest.approx(
                [float(row.lexical_score) for row in expected]
            )
            assert all(item.document_name != "Disabled Annual Leave.txt" for item in actual)
    engine.dispose()
