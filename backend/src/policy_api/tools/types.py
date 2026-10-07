from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlannedToolCall:
    call_id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class PlannerTurn:
    text: str | None
    tool_calls: tuple[PlannedToolCall, ...]
