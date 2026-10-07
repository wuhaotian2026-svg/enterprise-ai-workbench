from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from policy_api.models import StringEnum
from policy_api.slot_extraction.errors import SlotExtractionError


DRAFT_CONTROL_VERSION = "draft-control-v1"
MAX_CURRENT_USER_TURN_LENGTH = 2000

_CLEAR_PHRASES = frozenset(
    {
        "清空草稿",
        "清除草稿",
        "清除当前草稿",
        "重新开始",
    }
)
_CONFIRM_PHRASES = frozenset({"用新的", "使用新值", "替换成新值"})
_KEEP_PHRASES = frozenset({"保留原值", "用原来的", "保持原值"})


class DraftControlAction(StringEnum):
    CLEAR_DRAFT = "clear_draft"
    CONFIRM_REPLACE = "confirm_replace"
    KEEP_EXISTING = "keep_existing"


@dataclass(frozen=True, slots=True)
class DraftControl:
    version: Literal["draft-control-v1"]
    action: DraftControlAction
    field_name: str | None
    source_quote: str
    source_span: tuple[int, int]


def validate_current_user_turn_text(current_user_turn_text: str) -> str:
    if (
        not isinstance(current_user_turn_text, str)
        or not current_user_turn_text.strip()
        or len(current_user_turn_text) > MAX_CURRENT_USER_TURN_LENGTH
    ):
        raise SlotExtractionError("slot_extraction_request_invalid")
    return current_user_turn_text


def parse_draft_control(
    current_user_turn_text: str,
    *,
    pending_conflict_names: Collection[str],
    field_labels: Mapping[str, str],
) -> DraftControl | None:
    validate_current_user_turn_text(current_user_turn_text)
    text = current_user_turn_text.strip()
    start = current_user_turn_text.find(text)
    span = (start, start + len(text))

    if text in _CLEAR_PHRASES:
        return DraftControl(
            version=DRAFT_CONTROL_VERSION,
            action=DraftControlAction.CLEAR_DRAFT,
            field_name=None,
            source_quote=text,
            source_span=span,
        )

    conflicts = tuple(dict.fromkeys(pending_conflict_names))
    if len(conflicts) == 1 and text in _CONFIRM_PHRASES:
        return DraftControl(
            version=DRAFT_CONTROL_VERSION,
            action=DraftControlAction.CONFIRM_REPLACE,
            field_name=conflicts[0],
            source_quote=text,
            source_span=span,
        )
    if len(conflicts) == 1 and text in _KEEP_PHRASES:
        return DraftControl(
            version=DRAFT_CONTROL_VERSION,
            action=DraftControlAction.KEEP_EXISTING,
            field_name=conflicts[0],
            source_quote=text,
            source_span=span,
        )

    matches: list[tuple[DraftControlAction, str]] = []
    for field_name in conflicts:
        label = field_labels.get(field_name)
        if not label:
            continue
        if text in {
            f"{label}用新的",
            f"{label}使用新值",
            f"{label}替换成新值",
        }:
            matches.append((DraftControlAction.CONFIRM_REPLACE, field_name))
        if text in {
            f"保留原{label}",
            f"{label}保留原值",
            f"{label}用原来的",
        }:
            matches.append((DraftControlAction.KEEP_EXISTING, field_name))
    if len(matches) != 1:
        return None
    action, field_name = matches[0]
    return DraftControl(
        version=DRAFT_CONTROL_VERSION,
        action=action,
        field_name=field_name,
        source_quote=text,
        source_span=span,
    )


__all__ = [
    "DRAFT_CONTROL_VERSION",
    "MAX_CURRENT_USER_TURN_LENGTH",
    "DraftControl",
    "DraftControlAction",
    "parse_draft_control",
    "validate_current_user_turn_text",
]
