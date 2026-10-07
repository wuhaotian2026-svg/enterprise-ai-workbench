"""Versioned, domain-neutral approval process definitions."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from policy_api.approvals.enums import AssignmentKind
from policy_api.tools.errors import ToolError


@dataclass(frozen=True, slots=True)
class StepDefinition:
    key: str
    label: str
    sequence: int
    assignment_kind: AssignmentKind
    required_capability: str


@dataclass(frozen=True, slots=True)
class ProcessDefinition:
    process_key: str
    version: int
    steps: tuple[StepDefinition, ...]

    def __post_init__(self) -> None:
        try:
            frozen_steps = tuple(self.steps)
        except TypeError:
            raise ValueError("invalid_process_definition") from None
        object.__setattr__(self, "steps", frozen_steps)

        step_keys = [step.key for step in self.steps]
        step_sequences = [step.sequence for step in self.steps]
        expected_sequences = list(range(1, len(self.steps) + 1))
        complete_steps = all(
            isinstance(step, StepDefinition)
            and type(step.key) is str
            and bool(step.key.strip())
            and type(step.label) is str
            and bool(step.label.strip())
            and type(step.sequence) is int
            and step.sequence > 0
            and type(step.assignment_kind) is AssignmentKind
            and type(step.required_capability) is str
            and bool(step.required_capability.strip())
            for step in self.steps
        )
        if (
            type(self.process_key) is not str
            or not self.process_key.strip()
            or type(self.version) is not int
            or self.version < 1
            or not self.steps
            or not complete_steps
            or len(step_keys) != len(set(step_keys))
            or step_sequences != expected_sequences
        ):
            raise ValueError("invalid_process_definition")


@dataclass(frozen=True, slots=True)
class StepAssignment:
    kind: AssignmentKind
    assigned_user_id: uuid.UUID | None
    required_capability: str | None
    scope_organization_unit_id: uuid.UUID | None

    def __post_init__(self) -> None:
        is_user_shape = (
            self.kind is AssignmentKind.USER
            and isinstance(self.assigned_user_id, uuid.UUID)
            and self.required_capability is None
            and self.scope_organization_unit_id is None
        )
        is_capability_shape = (
            self.kind is AssignmentKind.CAPABILITY
            and self.assigned_user_id is None
            and type(self.required_capability) is str
            and bool(self.required_capability.strip())
            and isinstance(self.scope_organization_unit_id, uuid.UUID)
        )
        if not (is_user_shape or is_capability_shape):
            raise ToolError("invalid_step_assignment")

    @classmethod
    def for_user(cls, user_id: uuid.UUID) -> StepAssignment:
        return cls(
            kind=AssignmentKind.USER,
            assigned_user_id=user_id,
            required_capability=None,
            scope_organization_unit_id=None,
        )

    @classmethod
    def for_capability(
        cls,
        capability: str,
        scope_organization_unit_id: uuid.UUID,
    ) -> StepAssignment:
        return cls(
            kind=AssignmentKind.CAPABILITY,
            assigned_user_id=None,
            required_capability=capability,
            scope_organization_unit_id=scope_organization_unit_id,
        )


class ProcessDefinitionRegistry:
    def __init__(self, definitions: Iterable[ProcessDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, int], ProcessDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ProcessDefinition) -> None:
        key = (definition.process_key, definition.version)
        if key in self._definitions:
            raise ToolError("approval_process_already_registered")
        self._definitions[key] = definition

    def get(self, process_key: str, version: int) -> ProcessDefinition:
        try:
            return self._definitions[(process_key, version)]
        except KeyError:
            raise ToolError("approval_process_not_found") from None


__all__ = [
    "AssignmentKind",
    "ProcessDefinition",
    "ProcessDefinitionRegistry",
    "StepAssignment",
    "StepDefinition",
]
