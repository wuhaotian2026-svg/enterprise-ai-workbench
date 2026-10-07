from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from policy_api.models import Base, StringEnum, TimestampMixin, UUIDPrimaryKeyMixin


class DraftStatus(StringEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    CLEARED = "cleared"
    EXPIRED = "expired"


class AssistantFlowDraft(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assistant_flow_drafts"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "module_key",
            "conversation_id",
            "intent",
            name="uq_assistant_flow_draft_scope_intent",
        ),
        CheckConstraint("version > 0", name="ck_assistant_flow_draft_version_positive"),
        CheckConstraint(
            "status IN ('active', 'closed', 'cleared', 'expired')",
            name="ck_assistant_flow_draft_status",
        ),
        Index(
            "ix_assistant_flow_drafts_scope_status",
            "owner_user_id",
            "module_key",
            "conversation_id",
            "status",
        ),
        Index("ix_assistant_flow_drafts_expiry", "status", "expires_at"),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    module_key: Mapped[str] = mapped_column(String(80), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    intent: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    field_values: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    field_sources: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    pending_candidates: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
