from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

import pytest
from pydantic import ValidationError

from policy_api.hr.enums import LeaveTypeCode
from policy_api.workbench.catalog import DEFAULT_MODULE_KEYS
from policy_api.workbench.events import (
    EVENT_DIMENSION_MODELS,
    PRODUCT_MODULE_KEYS,
    EventInput,
    ProductEvent,
    ProductEventEmitter,
    ProductEventValidationError,
    validate_event_dimensions,
)


EVENT_SAMPLES: dict[str, dict[str, object]] = {
    "workbench_module_opened": {
        "entry_source": "navigation",
        "module_key": "knowledge",
    },
    "question_submitted": {"message_length_bucket": "0_50"},
    "question_answered": {
        "has_citations": True,
        "processing_time_bucket": "1s_3s",
    },
    "question_clarification_requested": {
        "question_count_bucket": "two",
        "has_citations": True,
        "processing_time_bucket": "1s_3s",
    },
    "question_abstained": {"error_code": "insufficient_evidence"},
    "answer_feedback_submitted": {"helpful": True},
    "hr_turn_submitted": {"message_length_bucket": "51_200"},
    "hr_intent_resolved": {
        "intent": "submit_leave_request",
        "clarification_required": False,
    },
    "tool_planned": {
        "tool_name": "hr.submit_leave_request",
        "risk_level": "write",
    },
    "tool_validation_failed": {
        "tool_name": "hr.submit_leave_request",
        "error_code": "arguments_invalid",
        "retryable": True,
    },
    "tool_read_succeeded": {
        "tool_name": "hr.get_my_leave_balances",
        "processing_time_bucket": "lt_1s",
    },
    "confirmation_shown": {
        "tool_name": "hr.submit_leave_request",
        "risk_level": "write",
    },
    "confirmation_confirmed": {"tool_name": "hr.submit_leave_request"},
    "confirmation_cancelled": {"tool_name": "hr.submit_leave_request"},
    "confirmation_expired": {"tool_name": "hr.submit_leave_request"},
    "leave_request_submitted": {
        "leave_type": "annual",
        "workday_count_bucket": "1_2",
    },
    "leave_request_reviewed": {
        "decision": "approved",
        "processing_time_bucket": "same_day",
    },
    "leave_request_cancelled": {"leave_type": "annual"},
    "hr_flow_error": {
        "error_code": "provider_unavailable",
        "retryable": True,
    },
    "procurement_request_submitted": {
        "stage": "submission",
        "channel": "manual",
        "item_count_bucket": "2_5",
        "amount_bucket": "1000_9999",
    },
    "procurement_request_withdrawn": {
        "stage": "withdrawal",
        "processing_time_bucket": "10s_24h",
    },
    "approval_task_approved": {
        "stage": "department_review",
        "processing_time_bucket": "24h_48h",
    },
    "approval_task_rejected": {
        "stage": "procurement_review",
        "processing_time_bucket": "gte_48h",
    },
    "procurement_request_completed": {
        "stage": "completion",
        "outcome": "approved",
        "processing_time_bucket": "gte_48h",
    },
    "procurement_flow_error": {
        "stage": "confirmation",
        "error_code": "approval_task_state_conflict",
        "outcome": "conflict",
    },
}


@pytest.mark.parametrize(
    ("event_name", "extra_dimensions"),
    [
        ("leave_request_submitted", {"workday_count_bucket": "1_2"}),
        ("leave_request_cancelled", {}),
    ],
)
def test_leave_event_dimensions_accept_every_formal_leave_type(
    event_name: str,
    extra_dimensions: dict[str, object],
) -> None:
    for leave_type in LeaveTypeCode:
        assert validate_event_dimensions(
            event_name,
            {"leave_type": leave_type.value, **extra_dimensions},
        )["leave_type"] == leave_type.value


@pytest.mark.parametrize(
    ("event_name", "extra_dimensions"),
    [
        ("leave_request_submitted", {"workday_count_bucket": "1_2"}),
        ("leave_request_cancelled", {}),
    ],
)
def test_leave_event_dimensions_reject_retired_sick_type(
    event_name: str,
    extra_dimensions: dict[str, object],
) -> None:
    with pytest.raises(
        ProductEventValidationError,
        match="event_dimensions_invalid",
    ):
        validate_event_dimensions(
            event_name,
            {"leave_type": "sick", **extra_dimensions},
        )


def test_product_event_has_stable_id_request_and_privacy_columns() -> None:
    columns = ProductEvent.__table__.columns
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in ProductEvent.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("event_id",) in unique_columns
    assert columns["request_id"].type.length == 120
    assert columns["actor_user_id"].nullable is True
    assert columns["organization_unit_id"].nullable is True
    assert isinstance(columns["dimensions"].type, JSONB)


def test_product_event_duration_and_query_indexes_are_fixed() -> None:
    assert "ck_product_event_duration_nonnegative" in {
        constraint.name
        for constraint in ProductEvent.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in ProductEvent.__table__.indexes
    }
    assert indexes["ix_product_events_name_occurred"] == (
        "event_name",
        "occurred_at",
    )
    assert indexes["ix_product_events_module_occurred"] == (
        "module_key",
        "occurred_at",
    )
    assert indexes["ix_product_events_actor_occurred"] == (
        "actor_user_id",
        "occurred_at",
    )
    assert indexes["ix_product_events_organization_occurred"] == (
        "organization_unit_id",
        "occurred_at",
    )


def test_product_event_catalog_has_independent_closed_dimension_models() -> None:
    assert set(EVENT_DIMENSION_MODELS) == set(EVENT_SAMPLES)
    assert len(set(EVENT_DIMENSION_MODELS.values())) == len(EVENT_SAMPLES)

    for event_name, dimensions in EVENT_SAMPLES.items():
        model = EVENT_DIMENSION_MODELS[event_name]
        assert model.model_config["extra"] == "forbid"
        assert model.model_validate(dimensions).model_dump(mode="json") == dimensions
        with pytest.raises(ValidationError):
            model.model_validate({**dimensions, "reason": "sensitive free text"})


@pytest.mark.parametrize("module_key", sorted(DEFAULT_MODULE_KEYS))
def test_module_opened_dimensions_accept_every_catalog_module(
    module_key: str,
) -> None:
    dimensions = {
        "entry_source": "navigation",
        "module_key": module_key,
    }

    assert EVENT_DIMENSION_MODELS["workbench_module_opened"].model_validate(
        dimensions
    ).model_dump(mode="json") == dimensions


def test_product_event_module_keys_match_the_workbench_catalog() -> None:
    assert PRODUCT_MODULE_KEYS == DEFAULT_MODULE_KEYS


@pytest.mark.parametrize(
    ("event_name", "dimensions"),
    (
        (
            "question_answered",
            {"has_citations": True, "processing_time_bucket": "10s_24h"},
        ),
        (
            "tool_read_succeeded",
            {
                "tool_name": "hr.get_my_leave_balances",
                "processing_time_bucket": "24h_48h",
            },
        ),
        (
            "leave_request_reviewed",
            {"decision": "approved", "processing_time_bucket": "gte_48h"},
        ),
    ),
)
def test_procurement_processing_buckets_are_rejected_by_non_procurement_events(
    event_name: str,
    dimensions: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EVENT_DIMENSION_MODELS[event_name].model_validate(dimensions)


@pytest.mark.parametrize(
    "event_name", ["question_abstained", "hr_flow_error", "procurement_flow_error"]
)
def test_error_dimensions_reject_free_text_codes(event_name: str) -> None:
    dimensions = dict(EVENT_SAMPLES[event_name])
    dimensions["error_code"] = "employee reason must never be stored"

    with pytest.raises(ValidationError):
        EVENT_DIMENSION_MODELS[event_name].model_validate(dimensions)


def procurement_event(**overrides: object) -> EventInput:
    values: dict[str, object] = {
        "event_id": __import__("uuid").uuid4(),
        "event_name": "procurement_request_submitted",
        "module_key": "procurement",
        "actor_user_id": __import__("uuid").uuid4(),
        "organization_unit_id": __import__("uuid").uuid4(),
        "role_snapshot": "employee",
        "request_id": "trace-procurement-1",
        "outcome": "succeeded",
        "duration_ms": None,
        "dimensions": dict(EVENT_SAMPLES["procurement_request_submitted"]),
    }
    values.update(overrides)
    return EventInput(**values)  # type: ignore[arg-type]


def test_procurement_module_and_submission_dimensions_are_closed() -> None:
    assert "procurement" in PRODUCT_MODULE_KEYS
    model = EVENT_DIMENSION_MODELS["procurement_request_submitted"]
    expected = EVENT_SAMPLES["procurement_request_submitted"]
    assert model.model_validate(expected).model_dump(mode="json") == expected

    for extra in ("title", "purpose", "item_name", "total_amount", "prompt", "token"):
        with pytest.raises(ValidationError):
            model.model_validate({**expected, extra: "sensitive"})

    ai_assisted = {**expected, "channel": "ai_confirmation"}
    assert model.model_validate(ai_assisted).channel == "ai_confirmation"


@pytest.mark.parametrize(
    "tool_name",
    [
        "procurement.submit_request",
        "procurement.withdraw_request",
        "approval.approve_task",
        "approval.reject_task",
    ],
)
def test_procurement_tool_names_are_closed_for_confirmation_analytics(
    tool_name: str,
) -> None:
    shown = EVENT_DIMENSION_MODELS["confirmation_shown"].model_validate(
        {"tool_name": tool_name, "risk_level": "write"}
    )
    confirmed = EVENT_DIMENSION_MODELS["confirmation_confirmed"].model_validate(
        {"tool_name": tool_name}
    )

    assert shown.tool_name == confirmed.tool_name == tool_name


def test_procurement_lifecycle_catalog_is_exact_and_low_sensitivity() -> None:
    procurement_events = {
        event_name
        for event_name in EVENT_DIMENSION_MODELS
        if event_name.startswith("procurement_") or event_name.startswith("approval_task_")
    }
    assert procurement_events == {
        "procurement_request_submitted",
        "procurement_request_withdrawn",
        "approval_task_approved",
        "approval_task_rejected",
        "procurement_request_completed",
        "procurement_flow_error",
    }

    forbidden = (
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
    )
    for event_name in procurement_events:
        model = EVENT_DIMENSION_MODELS[event_name]
        dimensions = EVENT_SAMPLES[event_name]
        for key in forbidden:
            with pytest.raises(ValidationError):
                model.model_validate({**dimensions, key: "sensitive"})


@pytest.mark.parametrize(
    ("event_name", "field", "value"),
    [
        ("procurement_request_withdrawn", "stage", "free text"),
        ("approval_task_approved", "stage", "executive_review"),
        ("approval_task_rejected", "processing_time_bucket", "1234ms"),
        ("procurement_request_completed", "outcome", "maybe"),
        ("procurement_flow_error", "outcome", "raw stack trace"),
    ],
)
def test_procurement_lifecycle_rejects_unregistered_dimension_values(
    event_name: str, field: str, value: str
) -> None:
    dimensions = {**EVENT_SAMPLES[event_name], field: value}

    with pytest.raises(ValidationError):
        EVENT_DIMENSION_MODELS[event_name].model_validate(dimensions)


@pytest.mark.parametrize(
    "dimensions",
    [
        {"stage": "draft", "channel": "manual", "item_count_bucket": "2_5", "amount_bucket": "1000_9999"},
        {"stage": "submission", "channel": "manual", "item_count_bucket": "51_plus", "amount_bucket": "1000_9999"},
        {"stage": "submission", "channel": "manual", "item_count_bucket": "2_5", "amount_bucket": "1234.56"},
        {"stage": "submission", "channel": "chat_guess", "item_count_bucket": "2_5", "amount_bucket": "1000_9999"},
    ],
)
def test_procurement_submission_rejects_unknown_dimension_values(
    dimensions: dict[str, object]
) -> None:
    emitter = ProductEventEmitter()
    db = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()

    with pytest.raises(ProductEventValidationError, match="event_dimensions_invalid"):
        emitter.append(db, procurement_event(dimensions=dimensions))

    db.scalar.assert_not_called()
    db.add.assert_not_called()


def test_unknown_event_and_module_contracts_remain_fail_closed() -> None:
    emitter = ProductEventEmitter()
    db = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()

    with pytest.raises(ProductEventValidationError, match="event_name_not_allowed"):
        emitter.append(db, procurement_event(event_name="procurement_payload_dumped"))
    with pytest.raises(ProductEventValidationError, match="event_module_invalid"):
        emitter.append(db, procurement_event(module_key="procurement-injected"))
