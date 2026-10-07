from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from policy_api.assistant_drafts.models import AssistantFlowDraft, DraftStatus
from policy_api.tools.errors import ToolError


@dataclass(frozen=True, slots=True)
class DraftSnapshot:
    id: UUID
    owner_user_id: UUID
    module_key: str
    conversation_id: UUID
    intent: str
    status: str
    version: int
    fields: dict[str, object]
    sources: dict[str, object]
    pending: dict[str, object]
    expires_at: datetime


class AssistantDraftStore:
    """Owner- and conversation-scoped persistence for explicit user fields."""

    def __init__(self, *, ttl: timedelta = timedelta(days=7)) -> None:
        self._ttl = ttl

    def get_active(
        self,
        db: Session,
        *,
        owner_user_id: UUID,
        module_key: str,
        conversation_id: UUID,
        intent: str | None = None,
        now: datetime | None = None,
        for_update: bool = False,
    ) -> DraftSnapshot | None:
        current = now or datetime.now(timezone.utc)
        statement = select(AssistantFlowDraft).where(
            AssistantFlowDraft.owner_user_id == owner_user_id,
            AssistantFlowDraft.module_key == module_key,
            AssistantFlowDraft.conversation_id == conversation_id,
            AssistantFlowDraft.status == DraftStatus.ACTIVE.value,
        )
        if intent is not None:
            statement = statement.where(AssistantFlowDraft.intent == intent)
        statement = statement.order_by(AssistantFlowDraft.updated_at.desc())
        if for_update:
            statement = statement.with_for_update()
        row = db.scalar(statement)
        if row is None:
            return None
        if row.expires_at <= current:
            row.status = DraftStatus.EXPIRED.value
            row.version += 1
            db.flush()
            return None
        return self._snapshot(row)

    def save(
        self,
        db: Session,
        *,
        owner_user_id: UUID,
        module_key: str,
        conversation_id: UUID,
        intent: str,
        fields: Mapping[str, object],
        sources: Mapping[str, object],
        pending: Mapping[str, object],
        now: datetime | None = None,
    ) -> DraftSnapshot:
        current = now or datetime.now(timezone.utc)
        row = db.scalar(
            select(AssistantFlowDraft)
            .where(
                AssistantFlowDraft.owner_user_id == owner_user_id,
                AssistantFlowDraft.module_key == module_key,
                AssistantFlowDraft.conversation_id == conversation_id,
                AssistantFlowDraft.intent == intent,
            )
            .with_for_update()
        )
        if row is None:
            row = AssistantFlowDraft(
                owner_user_id=owner_user_id,
                module_key=module_key,
                conversation_id=conversation_id,
                intent=intent,
                status=DraftStatus.ACTIVE.value,
                version=1,
                field_values=dict(fields),
                field_sources=dict(sources),
                pending_candidates=dict(pending),
                expires_at=current + self._ttl,
            )
            db.add(row)
        else:
            if (
                row.status == DraftStatus.ACTIVE.value
                and row.field_values == dict(fields)
                and row.field_sources == dict(sources)
                and row.pending_candidates == dict(pending)
            ):
                return self._snapshot(row)
            row.status = DraftStatus.ACTIVE.value
            row.version += 1
            row.field_values = dict(fields)
            row.field_sources = dict(sources)
            row.pending_candidates = dict(pending)
            row.expires_at = current + self._ttl
        db.flush()
        return self._snapshot(row)

    def assert_current_version(
        self,
        db: Session,
        *,
        draft_id: UUID,
        expected_version: int,
    ) -> DraftSnapshot:
        row = db.scalar(
            select(AssistantFlowDraft)
            .where(
                AssistantFlowDraft.id == draft_id,
                AssistantFlowDraft.status == DraftStatus.ACTIVE.value,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.version != expected_version:
            raise ToolError("assistant_draft_version_conflict")
        return self._snapshot(row)

    def transition(
        self,
        db: Session,
        *,
        owner_user_id: UUID,
        module_key: str,
        conversation_id: UUID,
        status: DraftStatus,
    ) -> int:
        rows = db.scalars(
            select(AssistantFlowDraft)
            .where(
                AssistantFlowDraft.owner_user_id == owner_user_id,
                AssistantFlowDraft.module_key == module_key,
                AssistantFlowDraft.conversation_id == conversation_id,
                AssistantFlowDraft.status == DraftStatus.ACTIVE.value,
            )
            .with_for_update()
        ).all()
        for row in rows:
            row.status = status.value
            row.version += 1
        db.flush()
        return len(rows)

    @staticmethod
    def _snapshot(row: AssistantFlowDraft) -> DraftSnapshot:
        return DraftSnapshot(
            id=row.id,
            owner_user_id=row.owner_user_id,
            module_key=row.module_key,
            conversation_id=row.conversation_id,
            intent=row.intent,
            status=row.status,
            version=row.version,
            fields=dict(row.field_values),
            sources=dict(row.field_sources),
            pending=dict(row.pending_candidates),
            expires_at=row.expires_at,
        )
