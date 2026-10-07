from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from policy_api.database import assert_test_database_url
from policy_api.models import User, UserRole
from policy_api.slot_extraction.errors import SlotExtractionError
from policy_api.slot_extraction.fingerprint import (
    RequestFingerprint,
    SlotExtractionFingerprinter,
    SlotExtractionRequestIdentity,
)


NOW = datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)
MODEL_NAME = "deepseek-v4-flash"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def operation_engine():
    database_url = os.getenv("TEST_DATABASE_URL", "")
    if not database_url:
        pytest.fail("TEST_DATABASE_URL is required for operation repository tests")
    assert_test_database_url(database_url)
    previous_url = os.environ.get("DATABASE_URL")
    config = Config(str(_backend_root() / "alembic.ini"))
    config.set_main_option("script_location", str(_backend_root() / "alembic"))
    os.environ["DATABASE_URL"] = database_url
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "head")
        yield engine
    finally:
        command.upgrade(config, "head")
        engine.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


@pytest.fixture()
def db(operation_engine) -> Session:
    with operation_engine.connect() as connection:
        outer_transaction = connection.begin()
        session = Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            session.close()
            if outer_transaction.is_active:
                outer_transaction.rollback()


def _request(
    db: Session,
    *,
    client_turn_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    text: str = "我要请年假",
) -> SlotExtractionRequestIdentity:
    owner_user_id = uuid.uuid4()
    db.add(
        User(
            id=owner_user_id,
            username=f"slot-owner-{owner_user_id}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
    )
    db.flush()
    return SlotExtractionRequestIdentity(
        owner_user_id=owner_user_id,
        module_key="hr",
        conversation_id=conversation_id or uuid.uuid4(),
        client_turn_id=client_turn_id or uuid.uuid4(),
        slot_schema_version="slot-extraction-v1",
        slot_schema_sha256="b" * 64,
        current_user_turn_text=text,
    )


def _fingerprint(request: SlotExtractionRequestIdentity) -> RequestFingerprint:
    return SlotExtractionFingerprinter(b"s" * 32).fingerprint(request)


def _repository():
    from policy_api.slot_extraction.repository import (
        SlotExtractionOperationRepository,
    )

    return SlotExtractionOperationRepository()


def test_same_turn_claim_is_unique_and_never_redispatched(db: Session) -> None:
    request = _request(db)
    fingerprint = _fingerprint(request)
    repository = _repository()

    first = repository.claim(
        db,
        request,
        fingerprint,
        model_name=MODEL_NAME,
        now=NOW,
        timeout_seconds=30,
    )
    db.commit()
    second = repository.claim(
        db,
        request,
        fingerprint,
        model_name=MODEL_NAME,
        now=NOW,
        timeout_seconds=30,
    )

    assert first.claimed is True
    assert second.claimed is False
    assert second.operation.id == first.operation.id


def test_same_turn_different_hmac_is_conflict_without_dispatch(db: Session) -> None:
    request = _request(db)
    fingerprint = _fingerprint(request)
    repository = _repository()
    repository.claim(
        db,
        request,
        fingerprint,
        model_name=MODEL_NAME,
        now=NOW,
        timeout_seconds=30,
    )
    db.commit()

    with pytest.raises(SlotExtractionError, match="client_turn_id_conflict"):
        repository.claim(
            db,
            request,
            RequestFingerprint(value="a" * 64, key_id=fingerprint.key_id),
            model_name=MODEL_NAME,
            now=NOW,
            timeout_seconds=30,
        )


def test_same_turn_changed_fingerprint_key_fails_closed(db: Session) -> None:
    request = _request(db)
    fingerprint = _fingerprint(request)
    repository = _repository()
    repository.claim(
        db,
        request,
        fingerprint,
        model_name=MODEL_NAME,
        now=NOW,
        timeout_seconds=30,
    )
    db.commit()

    with pytest.raises(
        SlotExtractionError,
        match="slot_extraction_fingerprint_key_changed",
    ):
        repository.claim(
            db,
            request,
            RequestFingerprint(value=fingerprint.value, key_id="rotated-key-v2"),
            model_name=MODEL_NAME,
            now=NOW,
            timeout_seconds=30,
        )


@pytest.mark.parametrize("dispatched", [False, True])
def test_expired_inflight_operation_converges_to_indeterminate_without_redispatch(
    db: Session,
    dispatched: bool,
) -> None:
    request = _request(db)
    fingerprint = _fingerprint(request)
    repository = _repository()
    claimed = repository.claim(
        db,
        request,
        fingerprint,
        model_name=MODEL_NAME,
        now=NOW - timedelta(seconds=60),
        timeout_seconds=10,
    )
    if dispatched:
        repository.mark_dispatched(
            db,
            claimed.operation.id,
            now=NOW - timedelta(seconds=60),
            timeout_seconds=10,
        )
    db.commit()

    changed = repository.converge_stale(
        db,
        now=NOW,
        operation_id=claimed.operation.id,
    )
    db.commit()
    replay = repository.claim(
        db,
        request,
        fingerprint,
        model_name=MODEL_NAME,
        now=NOW,
        timeout_seconds=30,
    )

    assert changed == 1
    assert replay.claimed is False
    assert replay.operation.status.value == "indeterminate"
    assert replay.operation.error_code == "slot_extraction_outcome_indeterminate"


def test_late_provider_result_after_indeterminate_cannot_complete(db: Session) -> None:
    request = _request(db)
    repository = _repository()
    claimed = repository.claim(
        db,
        request,
        _fingerprint(request),
        model_name=MODEL_NAME,
        now=NOW - timedelta(seconds=60),
        timeout_seconds=10,
    )
    repository.mark_dispatched(
        db,
        claimed.operation.id,
        now=NOW - timedelta(seconds=60),
        timeout_seconds=10,
    )
    repository.converge_stale(db, now=NOW, operation_id=claimed.operation.id)
    db.commit()

    with pytest.raises(
        SlotExtractionError,
        match="slot_extraction_outcome_indeterminate",
    ):
        repository.complete_success(
            db,
            claimed.operation.id,
            accepted=1,
            pending=0,
            rejected=0,
            now=NOW + timedelta(seconds=1),
        )


def test_dispatched_operation_can_complete_once_and_replays_terminal_result(
    db: Session,
) -> None:
    request = _request(db)
    fingerprint = _fingerprint(request)
    repository = _repository()
    claimed = repository.claim(
        db,
        request,
        fingerprint,
        model_name=MODEL_NAME,
        now=NOW,
        timeout_seconds=30,
    )
    repository.mark_dispatched(
        db,
        claimed.operation.id,
        now=NOW,
        timeout_seconds=30,
    )
    completed = repository.complete_success(
        db,
        claimed.operation.id,
        accepted=2,
        pending=1,
        rejected=3,
        now=NOW + timedelta(seconds=1),
    )
    db.commit()
    replay = repository.claim(
        db,
        request,
        fingerprint,
        model_name=MODEL_NAME,
        now=NOW + timedelta(seconds=2),
        timeout_seconds=30,
    )

    assert completed.status.value == "succeeded"
    assert (completed.accepted_count, completed.pending_count, completed.rejected_count) == (
        2,
        1,
        3,
    )
    assert replay.claimed is False
    assert replay.operation.status.value == "succeeded"


def test_provider_failure_is_terminal_and_cannot_be_dispatched_again(db: Session) -> None:
    request = _request(db)
    fingerprint = _fingerprint(request)
    repository = _repository()
    claimed = repository.claim(
        db,
        request,
        fingerprint,
        model_name=MODEL_NAME,
        now=NOW,
        timeout_seconds=30,
    )
    repository.mark_dispatched(
        db,
        claimed.operation.id,
        now=NOW,
        timeout_seconds=30,
    )
    failed = repository.complete_failure(
        db,
        claimed.operation.id,
        error_code="slot_extraction_timeout",
        latency_ms=3000,
        now=NOW + timedelta(seconds=3),
    )
    db.commit()

    assert failed.status.value == "failed"
    assert failed.error_code == "slot_extraction_timeout"
    assert failed.latency_ms == 3000
    with pytest.raises(SlotExtractionError, match="slot_extraction_transition_invalid"):
        repository.mark_dispatched(
            db,
            claimed.operation.id,
            now=NOW + timedelta(seconds=4),
            timeout_seconds=30,
        )
