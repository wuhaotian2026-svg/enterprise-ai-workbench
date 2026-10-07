from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError

import pytest

from policy_api.approvals.definitions import (
    AssignmentKind,
    ProcessDefinition,
    ProcessDefinitionRegistry,
    StepAssignment,
    StepDefinition,
)
from policy_api.tools.errors import ToolError


def two_step_definition(*, process_key: str = "expense.request", version: int = 1) -> ProcessDefinition:
    return ProcessDefinition(
        process_key=process_key,
        version=version,
        steps=(
            StepDefinition(
                "manager_review",
                "Manager review",
                1,
                AssignmentKind.USER,
                "expense.department.review",
            ),
            StepDefinition(
                "specialist_review",
                "Specialist review",
                2,
                AssignmentKind.CAPABILITY,
                "expense.final.review",
            ),
        ),
    )


def test_registry_resolves_process_by_key_and_version() -> None:
    definition_v1 = two_step_definition(version=1)
    definition_v2 = two_step_definition(version=2)
    registry = ProcessDefinitionRegistry((definition_v1, definition_v2))

    assert registry.get("expense.request", 1) is definition_v1
    assert registry.get("expense.request", 2) is definition_v2


def test_registry_rejects_duplicate_process_key_and_version() -> None:
    definition = two_step_definition()

    with pytest.raises(ToolError) as error:
        ProcessDefinitionRegistry((definition, definition))

    assert error.value.code == "approval_process_already_registered"


def test_registry_returns_stable_error_for_unknown_version() -> None:
    registry = ProcessDefinitionRegistry((two_step_definition(),))

    with pytest.raises(ToolError) as error:
        registry.get("expense.request", 2)

    assert error.value.code == "approval_process_not_found"


def test_definition_defensively_freezes_steps_and_registry_result() -> None:
    source_steps = [
        StepDefinition("review", "Review", 1, AssignmentKind.USER, "expense.review")
    ]
    definition = ProcessDefinition(
        process_key="expense.request",
        version=1,
        steps=source_steps,  # type: ignore[arg-type]
    )
    registry = ProcessDefinitionRegistry((definition,))

    source_steps.append(
        StepDefinition("later", "Later", 2, AssignmentKind.USER, "expense.review")
    )
    resolved = registry.get("expense.request", 1)

    assert isinstance(resolved.steps, tuple)
    assert [step.key for step in resolved.steps] == ["review"]
    assert hash(resolved)
    with pytest.raises(FrozenInstanceError):
        resolved.version = 2  # type: ignore[misc]
    with pytest.raises(AttributeError):
        resolved.steps.append(source_steps[-1])  # type: ignore[attr-defined]


@pytest.mark.parametrize("version", [True, False, 0, -1, 1.0])
def test_definition_requires_a_strictly_positive_integer_version(version: object) -> None:
    with pytest.raises(ValueError, match="invalid_process_definition"):
        ProcessDefinition(
            process_key="expense.request",
            version=version,  # type: ignore[arg-type]
            steps=(
                StepDefinition(
                    "review", "Review", 1, AssignmentKind.USER, "expense.review"
                ),
            ),
        )


def test_definition_rejects_boolean_step_sequence() -> None:
    with pytest.raises(ValueError, match="invalid_process_definition"):
        ProcessDefinition(
            process_key="expense.request",
            version=1,
            steps=(
                StepDefinition(
                    "review", "Review", True, AssignmentKind.USER, "expense.review"
                ),
            ),
        )


@pytest.mark.parametrize(
    "steps",
    [
        (),
        (
            StepDefinition("review", "Review", 1, AssignmentKind.USER, "expense.review"),
            StepDefinition("review", "Review again", 2, AssignmentKind.USER, "expense.review"),
        ),
        (
            StepDefinition("first", "First", 1, AssignmentKind.USER, "expense.review"),
            StepDefinition("second", "Second", 1, AssignmentKind.USER, "expense.review"),
        ),
        (
            StepDefinition("second", "Second", 2, AssignmentKind.USER, "expense.review"),
            StepDefinition("first", "First", 1, AssignmentKind.USER, "expense.review"),
        ),
    ],
)
def test_definition_rejects_missing_duplicate_or_out_of_order_steps(
    steps: tuple[StepDefinition, ...],
) -> None:
    with pytest.raises(ValueError, match="invalid_process_definition"):
        ProcessDefinition(process_key="expense.request", version=1, steps=steps)


@pytest.mark.parametrize(
    "step",
    [
        StepDefinition("", "Review", 1, AssignmentKind.USER, "expense.review"),
        StepDefinition("review", "", 1, AssignmentKind.USER, "expense.review"),
        StepDefinition("review", "Review", 0, AssignmentKind.USER, "expense.review"),
        StepDefinition("review", "Review", 1, AssignmentKind.USER, ""),
    ],
)
def test_definition_rejects_incomplete_step(step: StepDefinition) -> None:
    with pytest.raises(ValueError, match="invalid_process_definition"):
        ProcessDefinition(process_key="expense.request", version=1, steps=(step,))


def test_step_assignment_factories_produce_closed_assignment_shapes() -> None:
    manager_id = uuid.uuid4()
    unit_id = uuid.uuid4()

    assert StepAssignment.for_user(manager_id) == StepAssignment(
        kind=AssignmentKind.USER,
        assigned_user_id=manager_id,
        required_capability=None,
        scope_organization_unit_id=None,
    )
    assert StepAssignment.for_capability("expense.final.review", unit_id) == StepAssignment(
        kind=AssignmentKind.CAPABILITY,
        assigned_user_id=None,
        required_capability="expense.final.review",
        scope_organization_unit_id=unit_id,
    )


@pytest.mark.parametrize(
    "assignment_factory",
    [
        lambda: StepAssignment(  # type: ignore[arg-type]
            kind="user",
            assigned_user_id=uuid.uuid4(),
            required_capability=None,
            scope_organization_unit_id=None,
        ),
        lambda: StepAssignment.for_user("not-a-uuid"),  # type: ignore[arg-type]
        lambda: StepAssignment(
            kind=AssignmentKind.USER,
            assigned_user_id=uuid.uuid4(),
            required_capability="expense.review",
            scope_organization_unit_id=None,
        ),
        lambda: StepAssignment(
            kind=AssignmentKind.USER,
            assigned_user_id=uuid.uuid4(),
            required_capability=None,
            scope_organization_unit_id=uuid.uuid4(),
        ),
        lambda: StepAssignment.for_capability("   ", uuid.uuid4()),
        lambda: StepAssignment.for_capability("expense.review", "not-a-uuid"),  # type: ignore[arg-type]
        lambda: StepAssignment(
            kind=AssignmentKind.CAPABILITY,
            assigned_user_id=uuid.uuid4(),
            required_capability="expense.review",
            scope_organization_unit_id=uuid.uuid4(),
        ),
    ],
)
def test_step_assignment_rejects_invalid_direct_and_factory_shapes(
    assignment_factory: object,
) -> None:
    with pytest.raises(ToolError) as error:
        assignment_factory()  # type: ignore[operator]

    assert error.value.code == "invalid_step_assignment"
