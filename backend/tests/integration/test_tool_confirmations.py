from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from policy_api.database import assert_test_database_url
from policy_api.models import User, UserRole
from policy_api.tools.audit import AuditSummary
from policy_api.tools.confirmation import (
    ToolExecutionResource,
    cancel_tool_confirmation,
    canonical_arguments_hash,
    confirm_tool_execution,
    create_tool_confirmation,
    stage_tool_execution,
)
from policy_api.tools.enums import (
    ToolAuditEventKind,
    ToolConfirmationStatus,
    ToolInvocationStatus,
)
from policy_api.tools.errors import ToolError
from policy_api.tools.models import ToolAuditEvent, ToolConfirmation, ToolInvocation


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
def confirmation_session() -> tuple[sessionmaker[Session], uuid.UUID, uuid.UUID]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for confirmation tests")
    assert_test_database_url(TEST_DATABASE_URL)
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    original_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    command.upgrade(config, "head")
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    with sessions() as db:
        owner = User(
            username=f"confirmation-owner-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        stranger = User(
            username=f"confirmation-stranger-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        db.add_all([owner, stranger])
        db.commit()
        owner_id = owner.id
        stranger_id = stranger.id
    try:
        yield sessions, owner_id, stranger_id
    finally:
        with sessions() as db:
            actor_ids = [owner_id, stranger_id]
            db.execute(
                delete(ToolAuditEvent).where(ToolAuditEvent.actor_user_id.in_(actor_ids))
            )
            db.execute(
                delete(ToolConfirmation).where(
                    ToolConfirmation.owner_user_id.in_(actor_ids)
                )
            )
            db.execute(
                delete(ToolInvocation).where(
                    ToolInvocation.actor_user_id.in_(actor_ids)
                )
            )
            db.execute(delete(User).where(User.id.in_(actor_ids)))
            db.commit()
        engine.dispose()
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url


def create_invocation(db: Session, actor_user_id: uuid.UUID) -> ToolInvocation:
    invocation = ToolInvocation(
        conversation_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        actor_user_id=actor_user_id,
        provider_call_id=f"call-{uuid.uuid4()}",
        tool_name="hr.submit_leave_request",
        provider_tool_name="hr_submit_leave_request",
        risk_level="write",
        status=ToolInvocationStatus.VALIDATED,
        arguments_hash="0" * 64,
    )
    db.add(invocation)
    db.flush()
    return invocation


def create_pending(
    db: Session,
    owner_id: uuid.UUID,
    arguments: dict[str, object],
    *,
    expires_at: datetime | None = None,
) -> ToolConfirmation:
    invocation = create_invocation(db, owner_id)
    confirmation = create_tool_confirmation(
        db,
        invocation=invocation,
        owner_user_id=owner_id,
        normalized_arguments=arguments,
        preview={"type": "leave_request", **arguments},
        expires_at=expires_at
        or datetime.now(timezone.utc) + timedelta(minutes=10),
        audit_summary=AuditSummary.for_execution(
            tool_name=invocation.tool_name,
            risk_level="write",
            outcome="confirmation_created",
        ),
    )
    db.commit()
    return confirmation


def test_creation_uses_canonical_utf8_json_hash_and_pending_audit(
    confirmation_session: tuple[sessionmaker[Session], uuid.UUID, uuid.UUID],
) -> None:
    sessions, owner_id, _stranger_id = confirmation_session
    arguments = {"reason": "家庭事务", "days": 2, "nested": {"b": 2, "a": 1}}
    expected_json = json.dumps(
        arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    with sessions() as db:
        confirmation = create_pending(db, owner_id, arguments)
        assert confirmation.status == ToolConfirmationStatus.PENDING
        assert confirmation.arguments_hash == hashlib.sha256(expected_json).hexdigest()
        assert confirmation.arguments_hash == canonical_arguments_hash(arguments)
        assert confirmation.expires_at.tzinfo is not None
        event = db.scalar(
            select(ToolAuditEvent).where(
                ToolAuditEvent.confirmation_id == confirmation.id,
                ToolAuditEvent.event_kind == ToolAuditEventKind.CONFIRMATION_CREATED,
            )
        )
        assert event is not None
        assert "家庭事务" not in repr(event.summary)


def test_owner_expiry_cancellation_and_argument_mismatch_are_fail_closed(
    confirmation_session: tuple[sessionmaker[Session], uuid.UUID, uuid.UUID],
) -> None:
    sessions, owner_id, stranger_id = confirmation_session
    arguments = {"request_id": str(uuid.uuid4())}
    callback = lambda _db, _confirmation: ToolExecutionResource(
        resource_type="leave_request", resource_id=uuid.uuid4(), result_summary={}
    )
    with sessions() as db:
        owned = create_pending(db, owner_id, arguments)
        with pytest.raises(ToolError, match="confirmation_not_found"):
            confirm_tool_execution(
                db,
                actor_user_id=stranger_id,
                confirmation_id=owned.id,
                client_operation_id=uuid.uuid4(),
                normalized_arguments=arguments,
                execute=callback,
            )
        with pytest.raises(ToolError, match="confirmation_arguments_mismatch"):
            confirm_tool_execution(
                db,
                actor_user_id=owner_id,
                confirmation_id=owned.id,
                client_operation_id=uuid.uuid4(),
                normalized_arguments={"request_id": str(uuid.uuid4())},
                execute=callback,
            )

        expired = create_pending(
            db,
            owner_id,
            arguments,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        expired_callbacks: list[uuid.UUID] = []
        with pytest.raises(ToolError, match="confirmation_expired"):
            confirm_tool_execution(
                db,
                actor_user_id=owner_id,
                confirmation_id=expired.id,
                client_operation_id=uuid.uuid4(),
                normalized_arguments=arguments,
                execute=callback,
                on_expired=lambda _db, item: expired_callbacks.append(item.id),
            )
        db.refresh(expired)
        assert expired.status == ToolConfirmationStatus.EXPIRED
        assert expired_callbacks == [expired.id]

        cancelled = create_pending(db, owner_id, arguments)
        cancel_tool_confirmation(db, owner_id, cancelled.id)
        db.commit()
        with pytest.raises(ToolError, match="confirmation_cancelled"):
            confirm_tool_execution(
                db,
                actor_user_id=owner_id,
                confirmation_id=cancelled.id,
                client_operation_id=uuid.uuid4(),
                normalized_arguments=arguments,
                execute=callback,
            )


def test_same_operation_replays_and_cross_confirmation_reuse_conflicts(
    confirmation_session: tuple[sessionmaker[Session], uuid.UUID, uuid.UUID],
) -> None:
    sessions, owner_id, _stranger_id = confirmation_session
    arguments = {"start_date": "2031-01-06", "end_date": "2031-01-07"}
    operation_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    calls = 0

    def execute(_db: Session, _confirmation: ToolConfirmation) -> ToolExecutionResource:
        nonlocal calls
        calls += 1
        return ToolExecutionResource(
            resource_type="leave_request",
            resource_id=resource_id,
            result_summary={"status": "pending", "reason": "must-not-persist"},
        )

    with sessions() as db:
        first_confirmation = create_pending(db, owner_id, arguments)
        first = confirm_tool_execution(
            db,
            actor_user_id=owner_id,
            confirmation_id=first_confirmation.id,
            client_operation_id=operation_id,
            normalized_arguments=arguments,
            execute=execute,
        )
        replay = confirm_tool_execution(
            db,
            actor_user_id=owner_id,
            confirmation_id=first_confirmation.id,
            client_operation_id=operation_id,
            normalized_arguments=arguments,
            execute=execute,
        )
        assert first.resource_id == replay.resource_id == resource_id
        assert replay.replayed is True
        assert calls == 1
        with pytest.raises(ToolError, match="confirmation_already_used"):
            confirm_tool_execution(
                db,
                actor_user_id=owner_id,
                confirmation_id=first_confirmation.id,
                client_operation_id=uuid.uuid4(),
                normalized_arguments=arguments,
                execute=execute,
            )

        other = create_pending(db, owner_id, {"start_date": "2031-01-08"})
        with pytest.raises(ToolError, match="operation_id_conflict"):
            confirm_tool_execution(
                db,
                actor_user_id=owner_id,
                confirmation_id=other.id,
                client_operation_id=operation_id,
                normalized_arguments={"start_date": "2031-01-08"},
                execute=execute,
            )


def test_domain_failure_rolls_back_consumption_and_redacts_exception_and_audit(
    confirmation_session: tuple[sessionmaker[Session], uuid.UUID, uuid.UUID],
) -> None:
    sessions, owner_id, _stranger_id = confirmation_session
    secret = "secret-reason-do-not-log"
    arguments = {"reason": secret, "start_date": "2031-01-06"}
    operation_id = uuid.uuid4()

    def fail(db: Session, _confirmation: ToolConfirmation) -> ToolExecutionResource:
        owner = db.get(User, owner_id)
        assert owner is not None
        owner.is_active = False
        raise RuntimeError(secret)

    with sessions() as db:
        confirmation = create_pending(db, owner_id, arguments)
        with pytest.raises(ToolError) as raised:
            confirm_tool_execution(
                db,
                actor_user_id=owner_id,
                confirmation_id=confirmation.id,
                client_operation_id=operation_id,
                normalized_arguments=arguments,
                execute=fail,
                failure_summary=AuditSummary.for_execution(
                    tool_name="hr.submit_leave_request",
                    risk_level="write",
                    outcome="failed",
                    operation_id=operation_id,
                    reason=secret,
                ),
            )
        assert raised.value.code == "tool_execution_failed"
        assert secret not in str(raised.value)
        db.refresh(confirmation)
        owner = db.get(User, owner_id)
        assert owner is not None and owner.is_active is True
        assert confirmation.status == ToolConfirmationStatus.PENDING
        assert confirmation.client_operation_id is None
        event = db.scalar(
            select(ToolAuditEvent).where(
                ToolAuditEvent.invocation_id == confirmation.invocation_id,
                ToolAuditEvent.event_kind == ToolAuditEventKind.EXECUTION_FAILED,
            )
        )
        assert event is not None
        assert secret not in repr(event.summary)


def test_final_event_callback_failure_rolls_back_execution_before_compensation(
    confirmation_session: tuple[sessionmaker[Session], uuid.UUID, uuid.UUID],
) -> None:
    sessions, owner_id, _stranger_id = confirmation_session
    arguments = {"request_id": str(uuid.uuid4())}

    def execute(db: Session, _confirmation: ToolConfirmation) -> ToolExecutionResource:
        owner = db.get(User, owner_id)
        assert owner is not None
        owner.is_active = False
        return ToolExecutionResource(
            resource_type="leave_request", resource_id=uuid.uuid4(), result_summary={}
        )

    def reject_final_event(
        _db: Session, _confirmation: ToolConfirmation, _resource: ToolExecutionResource,
    ) -> None:
        raise RuntimeError("product_event_invalid")

    with sessions() as db:
        confirmation = create_pending(db, owner_id, arguments)
        with pytest.raises(ToolError, match="tool_execution_failed"):
            confirm_tool_execution(
                db, actor_user_id=owner_id, confirmation_id=confirmation.id,
                client_operation_id=uuid.uuid4(), normalized_arguments=arguments,
                execute=execute, before_commit=reject_final_event,
            )
        db.refresh(confirmation)
        owner = db.get(User, owner_id)
        assert owner is not None and owner.is_active is True
        assert confirmation.status == ToolConfirmationStatus.PENDING
        assert confirmation.client_operation_id is None


def test_stage_tool_execution_never_commits_or_rolls_back() -> None:
    class TransactionSpy:
        commits = 0
        rollbacks = 0

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            self.rollbacks += 1

    db = TransactionSpy()
    resource_id = uuid.uuid4()
    confirmation_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    confirmation = ToolConfirmation(
        id=confirmation_id,
        invocation_id=uuid.uuid4(),
        owner_user_id=actor_id,
        tool_name="procurement.submit_request",
        normalized_arguments={"title": "办公用品"},
        arguments_hash=canonical_arguments_hash({"title": "办公用品"}),
        preview={},
        status=ToolConfirmationStatus.PENDING,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    class Invocation:
        id = confirmation.invocation_id
        risk_level = "write"
        status = ToolInvocationStatus.CONFIRMATION_PENDING
        result_resource_type = None
        result_resource_id = None

    invocation = Invocation()
    db.scalar = lambda _statement: confirmation  # type: ignore[attr-defined]
    db.get = lambda _model, _identity: invocation  # type: ignore[attr-defined]
    db.add = lambda _value: None  # type: ignore[attr-defined]
    db.flush = lambda: None  # type: ignore[attr-defined]

    result = stage_tool_execution(
        db,  # type: ignore[arg-type]
        actor_user_id=actor_id,
        confirmation_id=confirmation_id,
        client_operation_id=operation_id,
        normalized_arguments={"title": "办公用品"},
        execute=lambda _db, _confirmation: ToolExecutionResource(
            resource_type="procurement_request",
            resource_id=resource_id,
            result_summary={"status": "pending_manager"},
        ),
    )

    assert result.resource_id == resource_id
    assert (db.commits, db.rollbacks) == (0, 0)
    assert confirmation.status is ToolConfirmationStatus.CONSUMED
    assert invocation.status is ToolInvocationStatus.SUCCEEDED
