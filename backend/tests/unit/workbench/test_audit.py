from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from policy_api.workbench.audit import (
    SecurityAuditEvent,
    SecurityAuditValidationError,
    append_security_audit,
)
from policy_api.workbench.capabilities import Capability


def test_security_audit_event_has_only_the_approved_persistent_fields() -> None:
    columns = SecurityAuditEvent.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "event_name",
        "actor_user_id",
        "target_type",
        "target_id",
        "operation_id",
        "outcome",
        "request_id",
        "summary",
        "occurred_at",
        "created_at",
    }
    assert columns["request_id"].type.length == 120
    assert isinstance(columns["summary"].type, JSONB)
    assert columns["summary"].nullable is False


def test_security_audit_operation_is_unique_within_actor_scope() -> None:
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in SecurityAuditEvent.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("actor_user_id", "operation_id") in unique_columns


def audit_arguments() -> dict[str, object]:
    grant_id = uuid.uuid4()
    return {
        "event_name": "capability_grant_created",
        "actor_user_id": uuid.uuid4(),
        "target_type": "capability_grant",
        "target_id": grant_id,
        "operation_id": uuid.uuid4(),
        "outcome": "succeeded",
        "request_id": "trace-1",
        "summary": {
            "grant_id": grant_id,
            "capability": "organization.manage",
            "scope_kind": "global",
            "organization_unit_id": None,
        },
    }


def test_append_rejects_free_text_and_unknown_summary_keys_before_db_access() -> None:
    db = MagicMock(spec=Session)
    arguments = audit_arguments()
    arguments["summary"] = {"reason": "自由文本不允许"}

    with pytest.raises(
        SecurityAuditValidationError,
        match="security_audit_summary_invalid",
    ):
        append_security_audit(db, **arguments)

    db.scalar.assert_not_called()
    db.add.assert_not_called()
    db.flush.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("event_name", "prompt_injected_event", "security_audit_event_invalid"),
        ("target_type", "user", "security_audit_target_invalid"),
        ("outcome", "maybe", "security_audit_outcome_invalid"),
        ("request_id", "trace\nforged", "security_audit_request_id_invalid"),
    ],
)
def test_append_rejects_uncontrolled_audit_envelope_fields(
    field: str,
    value: str,
    code: str,
) -> None:
    db = MagicMock(spec=Session)
    arguments = audit_arguments()
    arguments[field] = value

    with pytest.raises(SecurityAuditValidationError, match=code):
        append_security_audit(db, **arguments)

    db.add.assert_not_called()


def test_append_rejects_control_characters_inside_allowed_summary_fields() -> None:
    db = MagicMock(spec=Session)
    arguments = audit_arguments()
    summary = arguments["summary"]
    assert isinstance(summary, dict)
    arguments["summary"] = {
        **summary,
        "capability": "organization.manage\nforged",
    }

    with pytest.raises(
        SecurityAuditValidationError,
        match="security_audit_summary_invalid",
    ):
        append_security_audit(db, **arguments)

    db.scalar.assert_not_called()
    db.add.assert_not_called()


def test_append_normalizes_uuid_summary_values_without_committing() -> None:
    db = MagicMock(spec=Session)
    db.scalar.return_value = None
    arguments = audit_arguments()

    event = append_security_audit(db, **arguments)

    summary = arguments["summary"]
    assert isinstance(summary, dict)
    assert event.summary == {
        **summary,
        "grant_id": str(summary["grant_id"]),
    }
    db.add.assert_called_once_with(event)
    db.flush.assert_called_once_with()
    db.commit.assert_not_called()


@pytest.mark.parametrize("capability", [item.value for item in Capability])
def test_append_accepts_every_trusted_capability_value(capability: str) -> None:
    db = MagicMock(spec=Session)
    db.scalar.return_value = None
    arguments = audit_arguments()
    summary = arguments["summary"]
    assert isinstance(summary, dict)
    arguments["summary"] = {**summary, "capability": capability}

    event = append_security_audit(db, **arguments)

    assert event.summary["capability"] == capability
    db.add.assert_called_once_with(event)
    db.flush.assert_called_once_with()


@pytest.mark.parametrize("capability", ["server.injected", 7, None])
def test_append_rejects_unknown_or_non_string_capability(
    capability: object,
) -> None:
    db = MagicMock(spec=Session)
    arguments = audit_arguments()
    summary = arguments["summary"]
    assert isinstance(summary, dict)
    arguments["summary"] = {**summary, "capability": capability}

    with pytest.raises(
        SecurityAuditValidationError,
        match="security_audit_summary_invalid",
    ):
        append_security_audit(db, **arguments)

    db.add.assert_not_called()
    db.flush.assert_not_called()


def procurement_audit_arguments() -> dict[str, object]:
    request_id = uuid.uuid4()
    return {
        "event_name": "procurement_request_submitted",
        "actor_user_id": uuid.uuid4(),
        "target_type": "procurement_request",
        "target_id": request_id,
        "operation_id": uuid.uuid4(),
        "outcome": "succeeded",
        "request_id": "trace-procurement-1",
        "summary": {
            "procurement_request_id": request_id,
            "approval_instance_id": uuid.uuid4(),
            "organization_unit_id": uuid.uuid4(),
            "item_count_bucket": "2_5",
            "amount_bucket": "1000_9999",
        },
    }


def test_procurement_submission_audit_accepts_only_closed_low_sensitivity_summary() -> None:
    db = MagicMock(spec=Session)
    db.scalar.return_value = None

    event = append_security_audit(db, **procurement_audit_arguments())

    assert set(event.summary) == {
        "procurement_request_id",
        "approval_instance_id",
        "organization_unit_id",
        "item_count_bucket",
        "amount_bucket",
    }
    assert all(isinstance(event.summary[key], str) for key in {
        "procurement_request_id", "approval_instance_id", "organization_unit_id"
    })
    db.commit.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "sensitive title"),
        ("purpose", "sensitive purpose"),
        ("item_name", "laptop"),
        ("specification", "16 GB"),
        ("total_amount", "1234.56"),
        ("quantity", "2.00"),
        ("prompt", "ignore policy"),
        ("token", "secret"),
    ],
)
def test_procurement_submission_audit_rejects_sensitive_or_unknown_summary_fields(
    field: str, value: str
) -> None:
    db = MagicMock(spec=Session)
    arguments = procurement_audit_arguments()
    summary = dict(arguments["summary"])
    summary[field] = value
    arguments["summary"] = summary

    with pytest.raises(SecurityAuditValidationError, match="security_audit_summary_invalid"):
        append_security_audit(db, **arguments)

    db.scalar.assert_not_called()
    db.add.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("item_count_bucket", "51_plus"),
        ("amount_bucket", "exact_1234.56"),
    ],
)
def test_procurement_submission_audit_rejects_unknown_bucket_values(
    field: str, value: str
) -> None:
    db = MagicMock(spec=Session)
    arguments = procurement_audit_arguments()
    summary = dict(arguments["summary"])
    summary[field] = value
    arguments["summary"] = summary

    with pytest.raises(SecurityAuditValidationError, match="security_audit_summary_invalid"):
        append_security_audit(db, **arguments)


PROCUREMENT_AUDIT_CASES = {
    "procurement_request_withdrawn": {
        "target_type": "procurement_request",
        "outcome": "succeeded",
        "summary": {
            "procurement_request_id": uuid.uuid4(),
            "approval_instance_id": uuid.uuid4(),
            "organization_unit_id": uuid.uuid4(),
            "stage": "withdrawal",
        },
    },
    "approval_task_approved": {
        "target_type": "procurement_request",
        "outcome": "succeeded",
        "summary": {
            "procurement_request_id": uuid.uuid4(),
            "approval_instance_id": uuid.uuid4(),
            "approval_task_id": uuid.uuid4(),
            "organization_unit_id": uuid.uuid4(),
            "stage": "department_review",
            "processing_time_bucket": "same_day",
        },
    },
    "approval_task_rejected": {
        "target_type": "procurement_request",
        "outcome": "succeeded",
        "summary": {
            "procurement_request_id": uuid.uuid4(),
            "approval_instance_id": uuid.uuid4(),
            "approval_task_id": uuid.uuid4(),
            "organization_unit_id": uuid.uuid4(),
            "stage": "procurement_review",
            "processing_time_bucket": "later",
        },
    },
    "procurement_operation_replayed": {
        "target_type": "procurement_request",
        "outcome": "replayed",
        "summary": {
            "procurement_request_id": uuid.uuid4(),
            "organization_unit_id": uuid.uuid4(),
            "stage": "submission",
            "code": "exact_replay",
        },
    },
    "procurement_operation_conflict": {
        "target_type": "procurement_request",
        "outcome": "conflict",
        "summary": {
            "procurement_request_id": uuid.uuid4(),
            "organization_unit_id": uuid.uuid4(),
            "stage": "procurement_review",
            "code": "approval_task_state_conflict",
        },
    },
    "procurement_permission_denied": {
        "target_type": "procurement_request",
        "outcome": "denied",
        "summary": {
            "procurement_request_id": uuid.uuid4(),
            "organization_unit_id": uuid.uuid4(),
            "stage": "department_review",
            "code": "approval_scope_denied",
        },
    },
    "procurement_confirmation_shown": {
        "target_type": "procurement_request",
        "target_id": None,
        "outcome": "shown",
        "summary": {
            "confirmation_id": uuid.uuid4(),
            "stage": "confirmation",
            "tool_name": "procurement.submit_request",
        },
    },
    "procurement_confirmation_confirmed": {
        "target_type": "procurement_request",
        "outcome": "confirmed",
        "summary": {
            "procurement_request_id": uuid.uuid4(),
            "organization_unit_id": uuid.uuid4(),
            "confirmation_id": uuid.uuid4(),
            "stage": "confirmation",
            "tool_name": "approval.approve_task",
        },
    },
    "procurement_confirmation_cancelled": {
        "target_type": "procurement_request",
        "target_id": None,
        "outcome": "cancelled",
        "summary": {
            "confirmation_id": uuid.uuid4(),
            "stage": "confirmation",
            "tool_name": "procurement.withdraw_request",
        },
    },
    "procurement_confirmation_expired": {
        "target_type": "procurement_request",
        "target_id": None,
        "outcome": "expired",
        "summary": {
            "confirmation_id": uuid.uuid4(),
            "stage": "confirmation",
            "tool_name": "approval.reject_task",
        },
    },
}


AUDIT_EXPECTED_OUTCOME = {
    "organization_unit_created": "succeeded",
    "organization_unit_updated": "succeeded",
    "organization_unit_deactivated": "succeeded",
    "employee_assignment_updated": "succeeded",
    "capability_grant_created": "succeeded",
    "capability_grant_revoked": "succeeded",
    "capability_grant_reactivated": "succeeded",
    "procurement_request_submitted": "succeeded",
    **{
        event_name: case["outcome"]
        for event_name, case in PROCUREMENT_AUDIT_CASES.items()
    },
}

AUDIT_TARGET_ID_REQUIREMENT = {
    **{event_name: "required" for event_name in AUDIT_EXPECTED_OUTCOME},
    "procurement_operation_replayed": "optional",
    "procurement_operation_conflict": "optional",
    "procurement_permission_denied": "optional",
    "procurement_confirmation_shown": "empty",
    "procurement_confirmation_cancelled": "empty",
    "procurement_confirmation_expired": "empty",
}

GLOBAL_AUDIT_CASES = {
    "organization_unit_created": {
        "target_type": "organization_unit",
        "summary": {
            "organization_unit_id": uuid.uuid4(),
            "parent_id": None,
            "changed_fields": ["code"],
        },
    },
    "organization_unit_updated": {
        "target_type": "organization_unit",
        "summary": {
            "organization_unit_id": uuid.uuid4(),
            "parent_id": None,
            "changed_fields": ["name"],
        },
    },
    "organization_unit_deactivated": {
        "target_type": "organization_unit",
        "summary": {
            "organization_unit_id": uuid.uuid4(),
            "changed_fields": ["is_active"],
        },
    },
    "employee_assignment_updated": {
        "target_type": "employee_assignment",
        "summary": {
            "employee_id": uuid.uuid4(),
            "organization_unit_id": uuid.uuid4(),
            "manager_employee_id": None,
            "changed_fields": ["organization_unit_id"],
        },
    },
    **{
        event_name: {
            "target_type": "capability_grant",
            "summary": {
                "grant_id": uuid.uuid4(),
                "capability": "organization.manage",
                "scope_kind": "global",
                "organization_unit_id": None,
            },
        }
        for event_name in (
            "capability_grant_created",
            "capability_grant_revoked",
            "capability_grant_reactivated",
        )
    },
}


def closed_audit_arguments(event_name: str) -> dict[str, object]:
    if event_name == "procurement_request_submitted":
        return procurement_audit_arguments()
    if event_name in PROCUREMENT_AUDIT_CASES:
        case = PROCUREMENT_AUDIT_CASES[event_name]
    else:
        case = GLOBAL_AUDIT_CASES[event_name]
    summary = dict(case["summary"])
    target_id = case.get("target_id")
    if "target_id" not in case:
        target_id = next(
            (
                summary[key]
                for key in (
                    "procurement_request_id",
                    "organization_unit_id",
                    "employee_id",
                    "grant_id",
                )
                if key in summary
            ),
            None,
        )
    return {
        "event_name": event_name,
        "actor_user_id": uuid.uuid4(),
        "target_type": case["target_type"],
        "target_id": target_id,
        "operation_id": uuid.uuid4(),
        "outcome": AUDIT_EXPECTED_OUTCOME[event_name],
        "request_id": "server-attempt",
        "summary": summary,
    }


@pytest.mark.parametrize(
    ("event_name", "wrong_outcome"),
    [
        (event_name, outcome)
        for event_name, expected in AUDIT_EXPECTED_OUTCOME.items()
        for outcome in (
            "succeeded",
            "replayed",
            "conflict",
            "denied",
            "shown",
            "confirmed",
            "cancelled",
            "expired",
        )
        if outcome != expected
    ],
)
def test_audit_rejects_every_cross_event_outcome_pair(
    event_name: str, wrong_outcome: str
) -> None:
    db = MagicMock(spec=Session)
    db.scalar.return_value = None
    arguments = closed_audit_arguments(event_name)
    arguments["outcome"] = wrong_outcome

    with pytest.raises(
        SecurityAuditValidationError, match="security_audit_outcome_invalid"
    ):
        append_security_audit(db, **arguments)

    db.scalar.assert_not_called()
    db.add.assert_not_called()


@pytest.mark.parametrize(
    ("event_name", "invalid_target_id"),
    [
        (
            event_name,
            None if requirement == "required" else uuid.uuid4(),
        )
        for event_name, requirement in AUDIT_TARGET_ID_REQUIREMENT.items()
        if requirement != "optional"
    ],
)
def test_audit_rejects_cross_event_target_presence(
    event_name: str, invalid_target_id: uuid.UUID | None
) -> None:
    db = MagicMock(spec=Session)
    db.scalar.return_value = None
    arguments = closed_audit_arguments(event_name)
    arguments["target_id"] = invalid_target_id

    with pytest.raises(
        SecurityAuditValidationError, match="security_audit_target_invalid"
    ):
        append_security_audit(db, **arguments)

    db.scalar.assert_not_called()
    db.add.assert_not_called()


@pytest.mark.parametrize(
    ("event_name", "outcome", "code"),
    [
        *(
            ("procurement_operation_conflict", "conflict", code)
            for code in (
                "operation_id_conflict",
                "procurement_request_conflict",
                "procurement_request_state_conflict",
                "approval_operation_id_conflict",
                "approval_instance_state_conflict",
                "approval_task_state_conflict",
            )
        ),
        *(
            ("procurement_permission_denied", "denied", code)
            for code in (
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
        ),
    ],
)
def test_procurement_attempt_audit_accepts_every_frozen_error_code(
    event_name: str,
    outcome: str,
    code: str,
) -> None:
    db = MagicMock(spec=Session)
    db.scalar.return_value = None
    append_security_audit(
        db,
        event_name=event_name,
        actor_user_id=uuid.uuid4(),
        target_type="procurement_request",
        target_id=None,
        operation_id=uuid.uuid4(),
        outcome=outcome,
        request_id="attempt-code-catalog",
        summary={
            "procurement_request_id": None,
            "organization_unit_id": None,
            "stage": "submission",
            "code": code,
        },
    )

    db.add.assert_called_once()


@pytest.mark.parametrize("event_name", PROCUREMENT_AUDIT_CASES)
def test_procurement_audit_catalog_accepts_each_closed_lifecycle_event(
    event_name: str,
) -> None:
    db = MagicMock(spec=Session)
    db.scalar.return_value = None
    case = PROCUREMENT_AUDIT_CASES[event_name]
    summary = case["summary"]
    assert isinstance(summary, dict)

    actor_user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    client_operation_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    attempt_request_id = "trace-procurement-lifecycle"
    outcome = case["outcome"]
    operation_id = (
        uuid.uuid5(
            uuid.UUID("f87c1e7e-b08d-5a36-a781-6ad3b35ea247"),
            f"{actor_user_id}:{client_operation_id}:{attempt_request_id}:{event_name}:{outcome}",
        )
        if outcome in {"replayed", "conflict", "denied"}
        else client_operation_id
    )
    event = append_security_audit(
        db,
        event_name=event_name,
        actor_user_id=actor_user_id,
        target_type=case["target_type"],
        target_id=case.get("target_id", summary.get("procurement_request_id")),
        operation_id=operation_id,
        outcome=outcome,
        request_id=attempt_request_id,
        summary=summary,
    )

    assert set(event.summary) == set(summary)
    assert all(
        isinstance(event.summary[key], str)
        for key in set(summary) & {
            "procurement_request_id",
            "approval_instance_id",
            "approval_task_id",
            "organization_unit_id",
            "confirmation_id",
        }
    )
    if outcome in {"replayed", "conflict", "denied"}:
        assert event.operation_id != client_operation_id


@pytest.mark.parametrize("event_name", PROCUREMENT_AUDIT_CASES)
@pytest.mark.parametrize(
    "field",
    [
        "title",
        "purpose",
        "item_name",
        "specification",
        "total_amount",
        "prompt",
        "tool_result",
        "token",
        "cookie",
        "password",
        "confirmation_arguments",
    ],
)
def test_procurement_audit_catalog_rejects_sensitive_payload_shapes(
    event_name: str, field: str
) -> None:
    db = MagicMock(spec=Session)
    case = PROCUREMENT_AUDIT_CASES[event_name]
    summary = case["summary"]
    assert isinstance(summary, dict)

    with pytest.raises(SecurityAuditValidationError, match="security_audit_summary_invalid"):
        append_security_audit(
            db,
            event_name=event_name,
            actor_user_id=uuid.uuid4(),
            target_type=case["target_type"],
            target_id=case.get("target_id", summary.get("procurement_request_id")),
            operation_id=uuid.uuid4(),
            outcome=case["outcome"],
            request_id="trace-procurement-lifecycle",
            summary={**summary, field: "sensitive"},
        )

    db.scalar.assert_not_called()
