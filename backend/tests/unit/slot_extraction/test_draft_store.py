from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest

from policy_api.assistant_drafts.models import AssistantFlowDraft, DraftStatus
from policy_api.assistant_drafts.store import AssistantDraftStore
from policy_api.tools.errors import ToolError


NOW = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)


class FakeSession:
    def __init__(self, row: AssistantFlowDraft | None) -> None:
        self.row = row
        self.flushes = 0

    def scalar(self, _statement):
        return self.row

    def flush(self) -> None:
        self.flushes += 1


def _row() -> AssistantFlowDraft:
    return AssistantFlowDraft(
        id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        module_key="hr",
        conversation_id=uuid.uuid4(),
        intent="submit_leave",
        status=DraftStatus.ACTIVE.value,
        version=3,
        field_values={"reason": "探亲"},
        field_sources={"reason": {"source_turn_id": "turn-1"}},
        pending_candidates={},
        expires_at=NOW + timedelta(days=1),
    )


def test_identical_save_is_version_noop() -> None:
    row = _row()
    db = FakeSession(row)
    snapshot = AssistantDraftStore().save(
        db,
        owner_user_id=row.owner_user_id,
        module_key=row.module_key,
        conversation_id=row.conversation_id,
        intent=row.intent,
        fields=row.field_values,
        sources=row.field_sources,
        pending=row.pending_candidates,
        now=NOW,
    )

    assert snapshot.version == 3
    assert row.version == 3


def test_assert_current_version_locks_and_returns_matching_snapshot() -> None:
    row = _row()
    snapshot = AssistantDraftStore().assert_current_version(
        FakeSession(row),
        draft_id=row.id,
        expected_version=3,
    )

    assert snapshot.id == row.id
    assert snapshot.version == 3


@pytest.mark.parametrize("row", [None, _row()])
def test_assert_current_version_fails_closed_for_missing_or_stale_draft(
    row: AssistantFlowDraft | None,
) -> None:
    expected = 99 if row is not None else 1
    with pytest.raises(ToolError, match="assistant_draft_version_conflict"):
        AssistantDraftStore().assert_current_version(
            FakeSession(row),
            draft_id=uuid.uuid4(),
            expected_version=expected,
        )
