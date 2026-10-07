from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from policy_api.procurement.observability import (
    ProcurementObservability,
    attempt_audit_operation_id,
    attempt_outcome_for_code,
    confirmation_audit_operation_id,
    lifecycle_event_id,
    processing_time_bucket,
)
from policy_api.procurement.runtime import ProcurementRuntime
from policy_api.tools.confirmation import StagedToolExecutionExpired


ACTOR = uuid.UUID("00000000-0000-0000-0000-000000000001")
OPERATION = uuid.UUID("00000000-0000-0000-0000-000000000002")
CONFIRMATION = uuid.UUID("00000000-0000-0000-0000-000000000003")
REQUEST = uuid.UUID("00000000-0000-0000-0000-000000000004")
ORGANIZATION = uuid.UUID("00000000-0000-0000-0000-000000000005")
TASK = uuid.UUID("00000000-0000-0000-0000-000000000006")
INSTANCE = uuid.UUID("00000000-0000-0000-0000-000000000007")
CONFLICT_CODES = (
    "operation_id_conflict",
    "procurement_request_conflict",
    "procurement_request_state_conflict",
    "approval_operation_id_conflict",
    "approval_instance_state_conflict",
    "approval_task_state_conflict",
)
DENIAL_CODES = (
    "procurement_profile_required",
    "procurement_manager_capability_required",
    "procurement_manager_unavailable",
    "approval_capability_required",
    "approval_scope_denied",
    "approval_task_not_assigned",
    "approval_assignment_mismatch",
    "procurement_request_not_found",
    "approval_task_not_found",
)


class StubRepository:
    def get_request(self, _db: object, request_id: uuid.UUID) -> object | None:
        if request_id != REQUEST:
            return None
        return type(
            "Request",
            (),
            {"id": REQUEST, "organization_unit_id": ORGANIZATION},
        )()

    def get_request_by_instance(
        self, _db: object, instance_id: uuid.UUID
    ) -> object | None:
        return self.get_request(_db, REQUEST) if instance_id == INSTANCE else None


class RecordingEmitter:
    def __init__(self) -> None:
        self.events: list[object] = []

    def append(self, _db: object, event: object) -> object:
        self.events.append(event)
        return event


class RecordingConfirmationObservability:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def stage_confirmation(self, _db: object, **values: object) -> None:
        self.calls.append(values)


class StubSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def get(self, _model: object, _identity: object) -> object:
        return SimpleNamespace(role=SimpleNamespace(value="employee"))

    def scalar(self, _statement: object) -> str:
        return "procurement.submit_request"

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class TaskSession:
    def get(self, _model: object, identity: object) -> object | None:
        if identity != TASK:
            return None
        return SimpleNamespace(
            instance_id=INSTANCE,
            step_key="procurement_review",
        )


def test_attempt_id_is_stable_separate_and_closed_by_outcome() -> None:
    replay = attempt_audit_operation_id(
        ACTOR, OPERATION, "server-attempt-1", "approval.approve", "replayed"
    )
    assert replay == attempt_audit_operation_id(
        ACTOR, OPERATION, "server-attempt-1", "approval.approve", "replayed"
    )
    assert replay != OPERATION
    assert replay != attempt_audit_operation_id(
        ACTOR, OPERATION, "server-attempt-1", "approval.approve", "conflict"
    )


def test_confirmation_id_is_stable_per_lifecycle_without_domain_collision() -> None:
    shown = confirmation_audit_operation_id(ACTOR, CONFIRMATION, "procurement_confirmation_shown")
    assert shown == confirmation_audit_operation_id(ACTOR, CONFIRMATION, "procurement_confirmation_shown")
    assert shown != confirmation_audit_operation_id(ACTOR, CONFIRMATION, "procurement_confirmation_confirmed")
    assert shown != lifecycle_event_id(ACTOR, OPERATION, "procurement_request_submitted")


@pytest.mark.parametrize("code", CONFLICT_CODES)
def test_attempt_classifier_accepts_every_frozen_conflict_code(code: str) -> None:
    assert attempt_outcome_for_code(code) == "conflict"


@pytest.mark.parametrize("code", DENIAL_CODES)
def test_attempt_classifier_accepts_every_frozen_denial_code(code: str) -> None:
    assert attempt_outcome_for_code(code) == "denied"


@pytest.mark.parametrize(
    "code",
    (
        "procurement_needed_date_invalid",
        "approval_decision_reason_required",
        "tool_arguments_invalid",
        "exact_replay",
    ),
)
def test_attempt_classifier_excludes_validation_and_replay(code: str) -> None:
    assert attempt_outcome_for_code(code) is None


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        *((code, "conflict") for code in CONFLICT_CODES),
        *((code, "denied") for code in DENIAL_CODES),
    ],
)
def test_every_frozen_failure_code_stages_attempt_audit_and_flow_error(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    outcome: str,
) -> None:
    audits: list[dict[str, Any]] = []
    emitter = RecordingEmitter()
    monkeypatch.setattr(
        "policy_api.procurement.observability.append_security_audit",
        lambda _db, **values: audits.append(values),
    )
    observability = ProcurementObservability(StubRepository(), emitter)  # type: ignore[arg-type]

    observability.stage_attempt(
        object(),  # type: ignore[arg-type]
        actor_user_id=ACTOR,
        actor_role="employee",
        client_operation_id=OPERATION,
        server_attempt_id=f"attempt-{code}",
        command_kind="procurement.submit",
        outcome=None,
        code=code,
        stage="submission",
    )

    assert audits[0]["outcome"] == outcome
    assert audits[0]["summary"]["code"] == code
    assert emitter.events[0].event_name == "procurement_flow_error"  # type: ignore[attr-defined]
    assert emitter.events[0].outcome == outcome  # type: ignore[attr-defined]


def test_validation_code_stages_no_procurement_attempt_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, Any]] = []
    emitter = RecordingEmitter()
    monkeypatch.setattr(
        "policy_api.procurement.observability.append_security_audit",
        lambda _db, **values: audits.append(values),
    )
    observability = ProcurementObservability(StubRepository(), emitter)  # type: ignore[arg-type]

    observability.stage_attempt(
        object(),  # type: ignore[arg-type]
        actor_user_id=ACTOR,
        actor_role="employee",
        client_operation_id=OPERATION,
        server_attempt_id="validation-attempt",
        command_kind="procurement.submit",
        outcome=None,
        code="procurement_needed_date_invalid",
        stage="submission",
    )

    assert audits == []
    assert emitter.events == []


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    (
        (timedelta(milliseconds=999), "lt_1s"),
        (timedelta(seconds=1), "1s_3s"),
        (timedelta(seconds=3), "3s_10s"),
        (timedelta(seconds=10), "10s_24h"),
        (timedelta(hours=24), "24h_48h"),
        (timedelta(hours=48), "gte_48h"),
    ),
)
def test_processing_time_bucket_labels_match_their_exact_ranges(
    elapsed: timedelta,
    expected: str,
) -> None:
    start = datetime(2026, 8, 24, tzinfo=timezone.utc)
    assert processing_time_bucket(start, start + elapsed) == expected


@pytest.mark.parametrize(
    ("outcome", "event_name"),
    (
        ("conflict", "procurement_operation_conflict"),
        ("denied", "procurement_permission_denied"),
    ),
)
def test_conflict_and_denial_stage_one_attempt_audit_and_one_flow_error(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    event_name: str,
) -> None:
    audits: list[dict[str, Any]] = []
    emitter = RecordingEmitter()
    monkeypatch.setattr(
        "policy_api.procurement.observability.append_security_audit",
        lambda _db, **values: audits.append(values),
    )
    observability = ProcurementObservability(StubRepository(), emitter)  # type: ignore[arg-type]

    observability.stage_attempt(
        object(),  # type: ignore[arg-type]
        actor_user_id=ACTOR,
        actor_role="employee",
        client_operation_id=OPERATION,
        server_attempt_id="server-attempt-1",
        command_kind="procurement.submit",
        outcome=outcome,
        code=(
            "operation_id_conflict"
            if outcome == "conflict"
            else "procurement_profile_required"
        ),
        stage="submission",
        request_id=REQUEST,
        organization_unit_id=ORGANIZATION,
    )

    assert len(audits) == 1
    assert audits[0]["event_name"] == event_name
    assert audits[0]["operation_id"] == attempt_audit_operation_id(
        ACTOR, OPERATION, "server-attempt-1", "procurement.submit", outcome
    )
    assert len(emitter.events) == 1
    flow_error = emitter.events[0]
    assert flow_error.event_name == "procurement_flow_error"  # type: ignore[attr-defined]
    assert flow_error.outcome == outcome  # type: ignore[attr-defined]
    assert flow_error.dimensions == {  # type: ignore[attr-defined]
        "stage": "submission",
        "error_code": audits[0]["summary"]["code"],
        "outcome": outcome,
    }


def test_exact_replay_is_deterministic_and_stages_audit_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, Any]] = []
    emitter = RecordingEmitter()
    monkeypatch.setattr(
        "policy_api.procurement.observability.append_security_audit",
        lambda _db, **values: audits.append(values),
    )
    observability = ProcurementObservability(StubRepository(), emitter)  # type: ignore[arg-type]
    arguments = {
        "actor_user_id": ACTOR,
        "actor_role": "employee",
        "client_operation_id": OPERATION,
        "server_attempt_id": "server-attempt-1",
        "command_kind": "procurement.submit",
        "outcome": "replayed",
        "code": "exact_replay",
        "stage": "submission",
        "request_id": REQUEST,
        "organization_unit_id": ORGANIZATION,
    }

    observability.stage_attempt(object(), **arguments)  # type: ignore[arg-type]
    observability.stage_attempt(object(), **arguments)  # type: ignore[arg-type]

    assert len(audits) == 2
    assert audits[0] == audits[1]
    assert emitter.events == []


def test_failed_approval_attempt_derives_authoritative_stage_and_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, Any]] = []
    emitter = RecordingEmitter()
    monkeypatch.setattr(
        "policy_api.procurement.observability.append_security_audit",
        lambda _db, **values: audits.append(values),
    )
    observability = ProcurementObservability(StubRepository(), emitter)  # type: ignore[arg-type]

    observability.stage_attempt(
        TaskSession(),  # type: ignore[arg-type]
        actor_user_id=ACTOR,
        actor_role="procurement_specialist",
        client_operation_id=OPERATION,
        server_attempt_id="approval-conflict-trace",
        command_kind="approval.approve",
        outcome="conflict",
        code="approval_task_state_conflict",
        stage=None,  # type: ignore[arg-type]
        approval_task_id=TASK,  # type: ignore[call-arg]
    )

    assert audits[0]["target_id"] == REQUEST
    assert audits[0]["summary"] == {
        "procurement_request_id": REQUEST,
        "organization_unit_id": ORGANIZATION,
        "stage": "procurement_review",
        "code": "approval_task_state_conflict",
    }
    assert emitter.events[0].organization_unit_id == ORGANIZATION  # type: ignore[attr-defined]
    assert emitter.events[0].dimensions["stage"] == "procurement_review"  # type: ignore[attr-defined]


def test_confirmation_evidence_contains_only_closed_low_sensitivity_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, Any]] = []
    emitter = RecordingEmitter()
    monkeypatch.setattr(
        "policy_api.procurement.observability.append_security_audit",
        lambda _db, **values: audits.append(values),
    )
    observability = ProcurementObservability(StubRepository(), emitter)  # type: ignore[arg-type]

    observability.stage_confirmation(
        object(),  # type: ignore[arg-type]
        actor_user_id=ACTOR,
        actor_role="employee",
        confirmation_id=CONFIRMATION,
        event_name="procurement_confirmation_confirmed",
        tool_name="procurement.submit_request",
        request_id="confirmation-trace",
        procurement_request_id=REQUEST,
    )

    assert audits[0]["summary"] == {
        "confirmation_id": CONFIRMATION,
        "stage": "confirmation",
        "tool_name": "procurement.submit_request",
        "procurement_request_id": REQUEST,
        "organization_unit_id": ORGANIZATION,
    }
    assert audits[0]["request_id"] == "confirmation-trace"
    assert emitter.events[0].request_id == "confirmation-trace"  # type: ignore[attr-defined]
    assert emitter.events[0].dimensions == {  # type: ignore[attr-defined]
        "tool_name": "procurement.submit_request"
    }
    serialized = repr((audits, emitter.events)).casefold()
    for forbidden in (
        "title",
        "purpose",
        "item_name",
        "specification",
        "comment",
        "reason",
        "payload",
        "arguments",
        "cookie",
        "password",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "event_name",
    (
        "procurement_confirmation_shown",
        "procurement_confirmation_confirmed",
        "procurement_confirmation_cancelled",
        "procurement_confirmation_expired",
    ),
)
def test_first_confirmation_lifecycle_keeps_authoritative_server_request_id(
    monkeypatch: pytest.MonkeyPatch,
    event_name: str,
) -> None:
    audits: list[dict[str, Any]] = []
    emitter = RecordingEmitter()
    monkeypatch.setattr(
        "policy_api.procurement.observability.append_security_audit",
        lambda _db, **values: audits.append(values),
    )
    observability = ProcurementObservability(StubRepository(), emitter)  # type: ignore[arg-type]

    observability.stage_confirmation(
        object(),  # type: ignore[arg-type]
        actor_user_id=ACTOR,
        actor_role="employee",
        confirmation_id=CONFIRMATION,
        event_name=event_name,
        tool_name="procurement.submit_request",
        request_id="authoritative-server-request",
        procurement_request_id=(
            REQUEST
            if event_name == "procurement_confirmation_confirmed"
            else None
        ),
    )

    assert audits[0]["request_id"] == "authoritative-server-request"
    assert emitter.events[0].request_id == "authoritative-server-request"  # type: ignore[attr-defined]


def test_cancel_confirmation_preserves_server_request_id_for_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = RecordingConfirmationObservability()
    confirmation = SimpleNamespace(
        id=CONFIRMATION,
        status=SimpleNamespace(value="cancelled"),
        tool_name="procurement.submit_request",
    )
    monkeypatch.setattr(
        "policy_api.procurement.runtime.cancel_tool_confirmation",
        lambda _db, _actor_id, _confirmation_id: confirmation,
    )
    runtime = ProcurementRuntime(
        service=object(),  # type: ignore[arg-type]
        capability_resolver=object(),  # type: ignore[arg-type]
        request_reader=object(),  # type: ignore[arg-type]
        observability=recorder,  # type: ignore[arg-type]
    )
    db = StubSession()

    result = runtime.cancel_confirmation(
        db,  # type: ignore[arg-type]
        ACTOR,
        CONFIRMATION,
        request_id="cancel-trace",
    )

    assert result == {
        "confirmation_id": CONFIRMATION,
        "status": "cancelled",
    }
    assert db.commits == 1
    assert db.rollbacks == 0
    assert recorder.calls[0]["request_id"] == "cancel-trace"


def test_cancel_expiration_stages_expired_evidence_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = RecordingConfirmationObservability()

    def expire(*_args: object) -> None:
        raise StagedToolExecutionExpired("confirmation_expired")

    monkeypatch.setattr(
        "policy_api.procurement.runtime.cancel_tool_confirmation",
        expire,
    )
    runtime = ProcurementRuntime(
        service=object(),  # type: ignore[arg-type]
        capability_resolver=object(),  # type: ignore[arg-type]
        request_reader=object(),  # type: ignore[arg-type]
        observability=recorder,  # type: ignore[arg-type]
    )
    db = StubSession()

    with pytest.raises(StagedToolExecutionExpired, match="confirmation_expired"):
        runtime.cancel_confirmation(
            db,  # type: ignore[arg-type]
            ACTOR,
            CONFIRMATION,
            request_id="expired-cancel-trace",
        )

    assert db.commits == 1
    assert db.rollbacks == 0
    assert recorder.calls == [
        {
            "actor_user_id": ACTOR,
            "actor_role": "employee",
            "confirmation_id": CONFIRMATION,
            "event_name": "procurement_confirmation_expired",
            "tool_name": "procurement.submit_request",
            "request_id": "expired-cancel-trace",
        }
    ]
