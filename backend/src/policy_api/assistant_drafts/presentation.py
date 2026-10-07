from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActionableClarificationProjection:
    """User-facing draft gaps derived from trusted internal draft state."""

    missing_fields: tuple[str, ...]
    pending_fields: tuple[str, ...]

    @property
    def clarification_fields(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys((*self.missing_fields, *self.pending_fields))
        )


def _pending_reason_code(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        reason_code = value.get("reason_code")
        if isinstance(reason_code, str):
            return reason_code
    return None


def project_actionable_clarification(
    *,
    missing_fields: tuple[str, ...],
    pending: Mapping[str, object],
    replacements: Mapping[tuple[str, str], str] | None = None,
    covered_pending_fields: Collection[str] = (),
) -> ActionableClarificationProjection:
    """Project internal pending state into fields the user can act on now.

    A replacement is selected only from a trusted field/reason-code pair. The
    internal pending value remains untouched; this function controls presentation
    only.
    """

    replacement_rules = replacements or {}
    pending_replacements: dict[str, str] = {}
    for name, value in pending.items():
        reason_code = _pending_reason_code(value)
        if reason_code is None:
            continue
        replacement = replacement_rules.get((name, reason_code))
        if replacement is not None:
            pending_replacements[name] = replacement

    projected_missing: list[str] = []
    for name in missing_fields:
        actionable_name = pending_replacements.get(name, name)
        if actionable_name not in projected_missing:
            projected_missing.append(actionable_name)
    for actionable_name in pending_replacements.values():
        if actionable_name not in projected_missing:
            projected_missing.append(actionable_name)

    covered = set(covered_pending_fields)
    projected_pending: list[str] = []
    for name in sorted(pending):
        if name in pending_replacements or name in covered:
            continue
        if name in projected_missing or name in missing_fields:
            continue
        projected_pending.append(name)

    return ActionableClarificationProjection(
        missing_fields=tuple(projected_missing),
        pending_fields=tuple(projected_pending),
    )
