from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
ORIGINAL_TABLES = {
    "users",
    "sessions",
    "documents",
    "ingestion_runs",
    "document_chunks",
    "questions",
    "answers",
    "answer_citations",
    "feedback",
    "evaluation_cases",
    "evaluation_runs",
    "evaluation_results",
}
HR_AND_TOOL_TABLES = {
    "employee_profiles",
    "leave_types",
    "leave_accounts",
    "leave_account_events",
    "work_calendar_days",
    "leave_requests",
    "hr_conversations",
    "hr_turns",
    "tool_invocations",
    "tool_confirmations",
    "tool_audit_events",
}
WORKBENCH_TABLES = {
    "organization_units",
    "capability_grants",
    "security_audit_events",
    "product_events",
}
PROCUREMENT_APPROVAL_TABLES = {
    "approval_instances",
    "approval_tasks",
    "approval_decisions",
    "procurement_requests",
    "procurement_request_items",
    "procurement_command_operations",
    "assistant_conversations",
    "assistant_turns",
    "approval_command_operations",
}


def test_rag_clarification_migration_fails_closed_without_pg_trgm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_root = Path(__file__).resolve().parents[2]
    migration_path = (
        backend_root
        / "alembic"
        / "versions"
        / "0004_rag_specific_query_clarification.py"
    )
    assert migration_path.exists()
    spec = importlib.util.spec_from_file_location("rag_clarification_migration", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class MissingPgTrgmConnection:
        @staticmethod
        def scalar(_statement) -> bool:
            return False

    monkeypatch.setattr(module.op, "get_bind", lambda: MissingPgTrgmConnection())
    with pytest.raises(RuntimeError, match="pg_trgm_extension_required"):
        module.upgrade()


def test_initial_migration_is_a_frozen_twelve_table_snapshot() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    source = (backend_root / "alembic" / "versions" / "0001_initial.py").read_text(
        encoding="utf-8"
    )

    assert "from policy_api.models import Base" not in source
    assert "metadata.create_all" not in source
    assert "metadata.drop_all" not in source
    created_tables = set(
        re.findall(r'op\.create_table\(\s*["\']([^"\']+)["\']', source)
    )
    assert created_tables == ORIGINAL_TABLES


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for PostgreSQL/pgvector migration verification",
)
def test_migrations_upgrade_and_downgrade_only_a_test_database() -> None:
    from policy_api.database import assert_test_database_url

    assert_test_database_url(TEST_DATABASE_URL)
    backend_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(backend_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_root / "alembic"))

    original_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    engine = create_engine(TEST_DATABASE_URL)

    try:
        command.downgrade(alembic_config, "base")
        command.upgrade(alembic_config, "0001_initial")

        table_names = set(inspect(engine).get_table_names())
        assert ORIGINAL_TABLES <= table_names

        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
            )
            assert connection.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_indexes "
                    "WHERE tablename = 'document_chunks' "
                    "AND indexname = 'ix_chunks_search_vector'"
                    ")"
                )
            )

        legacy_user_id = uuid.uuid4()
        legacy_document_id = uuid.uuid4()
        legacy_run_id = uuid.uuid4()
        legacy_chunk_id = uuid.uuid4()
        legacy_question_id = uuid.uuid4()
        legacy_answer_id = uuid.uuid4()
        legacy_citation_id = uuid.uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, password_hash, role, is_active, created_at, updated_at) "
                    "VALUES (:id, 'legacy-user', 'legacy-hash', 'employee', true, now(), now())"
                ),
                {"id": legacy_user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO documents "
                    "(id, display_name, storage_key, sha256, mime_type, status, is_enabled, "
                    "created_at, updated_at) "
                    "VALUES (:id, 'Legacy Policy', 'legacy-policy.pdf', :sha256, "
                    "'application/pdf', 'ready', true, now(), now())"
                ),
                {"id": legacy_document_id, "sha256": "a" * 64},
            )
            connection.execute(
                text(
                    "INSERT INTO ingestion_runs "
                    "(id, document_id, stage, status, parameters, created_at, updated_at) "
                    "VALUES (:id, :document_id, 'completed', 'completed', '{}', now(), now())"
                ),
                {"id": legacy_run_id, "document_id": legacy_document_id},
            )
            connection.execute(
                text(
                    "UPDATE documents SET active_run_id = :run_id WHERE id = :document_id"
                ),
                {"run_id": legacy_run_id, "document_id": legacy_document_id},
            )
            connection.execute(
                text(
                    "INSERT INTO document_chunks "
                    "(id, ingestion_run_id, document_id, sequence, text, text_hash, "
                    "created_at, updated_at) "
                    "VALUES (:id, :run_id, :document_id, 0, 'legacy evidence', :text_hash, "
                    "now(), now())"
                ),
                {
                    "id": legacy_chunk_id,
                    "run_id": legacy_run_id,
                    "document_id": legacy_document_id,
                    "text_hash": "b" * 64,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO questions "
                    "(id, user_id, text, status, created_at, updated_at) "
                    "VALUES (:id, :user_id, 'legacy question', 'completed', now(), now())"
                ),
                {"id": legacy_question_id, "user_id": legacy_user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO answers "
                    "(id, question_id, status, text, evidence_score, created_at, updated_at) "
                    "VALUES (:id, :question_id, 'answered', 'legacy answer', 0.9, now(), now())"
                ),
                {"id": legacy_answer_id, "question_id": legacy_question_id},
            )
            connection.execute(
                text(
                    "INSERT INTO answer_citations "
                    "(id, answer_id, chunk_id, citation_number, evidence_snapshot, "
                    "created_at, updated_at) "
                    "VALUES (:id, :answer_id, :chunk_id, 1, 'legacy evidence', now(), now())"
                ),
                {
                    "id": legacy_citation_id,
                    "answer_id": legacy_answer_id,
                    "chunk_id": legacy_chunk_id,
                },
            )

        command.upgrade(alembic_config, "head")
        command.upgrade(alembic_config, "head")
        assert (
            ORIGINAL_TABLES
            | HR_AND_TOOL_TABLES
            | WORKBENCH_TABLES
            | PROCUREMENT_APPROVAL_TABLES
            <= set(inspect(engine).get_table_names())
        )
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension "
                    "WHERE extname = 'pg_trgm')"
                )
            )
            assert connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                    "WHERE tablename = 'document_chunks' "
                    "AND indexname = 'ix_document_chunks_text_trgm')"
                )
            )
            assert connection.scalar(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_name = 'answers' AND column_name = 'status'"
                )
            ) >= 24
            assert connection.scalar(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_name = 'evaluation_cases' "
                    "AND column_name = 'expected_status'"
                )
            ) >= 24
            assert connection.scalar(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'answers' "
                    "AND column_name = 'clarification_payload'"
                )
            ) == "jsonb"
            assert connection.execute(
                text(
                    "SELECT username, password_hash, role, is_active FROM users WHERE id = :id"
                ),
                {"id": legacy_user_id},
            ).one() == ("legacy-user", "legacy-hash", "employee", True)
            assert connection.execute(
                text(
                    "SELECT display_name, storage_key, sha256, mime_type, status, is_enabled "
                    "FROM documents WHERE id = :id"
                ),
                {"id": legacy_document_id},
            ).one() == (
                "Legacy Policy",
                "legacy-policy.pdf",
                "a" * 64,
                "application/pdf",
                "ready",
                True,
            )
            assert connection.execute(
                text(
                    "SELECT q.text, a.status, a.text, a.clarification_payload, "
                    "c.evidence_snapshot FROM questions q "
                    "JOIN answers a ON a.question_id = q.id "
                    "JOIN answer_citations c ON c.answer_id = a.id "
                    "WHERE q.id = :question_id"
                ),
                {"question_id": legacy_question_id},
            ).one() == (
                "legacy question",
                "answered",
                "legacy answer",
                None,
                "legacy evidence",
            )

            clarification_question_id = uuid.uuid4()
            clarification_answer_id = uuid.uuid4()
            connection.execute(
                text(
                    "INSERT INTO questions "
                    "(id, user_id, text, status, created_at, updated_at) "
                    "VALUES (:id, :user_id, 'clarify', 'completed', now(), now())"
                ),
                {"id": clarification_question_id, "user_id": legacy_user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO answers "
                    "(id, question_id, status, text, clarification_payload, "
                    "created_at, updated_at) VALUES "
                    "(:id, :question_id, 'NEEDS_CLARIFICATION', 'rule', "
                    "CAST(:payload AS jsonb), now(), now())"
                ),
                {
                    "id": clarification_answer_id,
                    "question_id": clarification_question_id,
                    "payload": '{"questions":["which tier?"]}',
                },
            )
            assert connection.scalar(
                text(
                    "SELECT clarification_payload IS NOT NULL FROM answers "
                    "WHERE id = :id"
                ),
                {"id": clarification_answer_id},
            )

            invalid_savepoint = connection.begin_nested()
            try:
                with pytest.raises(IntegrityError):
                    connection.execute(
                        text(
                            "UPDATE answers SET clarification_payload = NULL "
                            "WHERE id = :id"
                        ),
                        {"id": clarification_answer_id},
                    )
            finally:
                invalid_savepoint.rollback()

        command.downgrade(alembic_config, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    finally:
        # This suite shares one fresh PostgreSQL database. Leave it at the
        # application migration head even when an assertion above fails so
        # later integration fixtures do not inherit a downgraded schema.
        command.upgrade(alembic_config, "head")
        engine.dispose()
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url
