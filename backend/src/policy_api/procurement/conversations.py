from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from policy_api.procurement.models import AssistantConversation, AssistantTurn
from policy_api.tools.errors import ToolError


MODULE_KEY = "procurement"


class ProcurementConversationStore:
    """Owner-scoped persistence for procurement assistant conversations."""

    def create(
        self,
        db: Session,
        *,
        owner_user_id: uuid.UUID,
        title: str | None,
    ) -> dict[str, object]:
        conversation = AssistantConversation(
            owner_user_id=owner_user_id,
            module_key=MODULE_KEY,
            title=(title or "新采购对话").strip() or "新采购对话",
            is_archived=False,
        )
        db.add(conversation)
        db.commit()
        return self._conversation_payload(conversation)

    def list(
        self, db: Session, *, owner_user_id: uuid.UUID
    ) -> list[dict[str, object]]:
        rows = db.scalars(
            select(AssistantConversation)
            .where(
                AssistantConversation.owner_user_id == owner_user_id,
                AssistantConversation.module_key == MODULE_KEY,
                AssistantConversation.is_archived.is_(False),
            )
            .order_by(AssistantConversation.updated_at.desc())
        )
        return [self._conversation_payload(row) for row in rows]

    def get(
        self,
        db: Session,
        *,
        owner_user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> dict[str, object]:
        conversation = self._owned(db, owner_user_id, conversation_id)
        turns = db.scalars(
            select(AssistantTurn)
            .where(
                AssistantTurn.conversation_id == conversation.id,
                AssistantTurn.owner_user_id == owner_user_id,
                AssistantTurn.module_key == MODULE_KEY,
            )
            .order_by(AssistantTurn.created_at, AssistantTurn.id)
        )
        return {
            **self._conversation_payload(conversation),
            "turns": [self._turn_payload(turn) for turn in turns],
        }

    def add_turn(
        self,
        db: Session,
        *,
        owner_user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        client_turn_id: uuid.UUID,
        text: str,
    ) -> dict[str, object]:
        conversation = self._owned(db, owner_user_id, conversation_id)
        canonical_text = text.strip()
        if not canonical_text:
            raise ToolError("turn_text_required")
        now = datetime.now(timezone.utc)
        turn_id = uuid.uuid4()
        claimed_id = db.scalar(
            insert(AssistantTurn)
            .values(
                id=turn_id,
                conversation_id=conversation.id,
                owner_user_id=owner_user_id,
                module_key=MODULE_KEY,
                client_turn_id=client_turn_id,
                role="user",
                request_content=canonical_text,
                content=canonical_text,
                blocks=[],
                model_name=None,
                latency_ms=None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["owner_user_id", "module_key", "client_turn_id"]
            )
            .returning(AssistantTurn.id)
        )
        replayed = claimed_id is None
        if replayed:
            turn = db.scalar(
                select(AssistantTurn).where(
                    AssistantTurn.owner_user_id == owner_user_id,
                    AssistantTurn.module_key == MODULE_KEY,
                    AssistantTurn.client_turn_id == client_turn_id,
                )
            )
            if turn is None or (
                turn.conversation_id != conversation_id
                or turn.request_content != canonical_text
            ):
                raise ToolError("client_turn_id_conflict")
        else:
            turn = db.get(AssistantTurn, claimed_id)
            if turn is None:
                raise ToolError("client_turn_id_conflict")
            conversation.updated_at = now
        db.commit()
        return self._turn_payload(turn, replayed=replayed)

    @staticmethod
    def _conversation_payload(
        conversation: AssistantConversation,
    ) -> dict[str, object]:
        return {
            "id": conversation.id,
            "title": conversation.title,
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at,
        }

    @staticmethod
    def _turn_payload(
        turn: AssistantTurn, *, replayed: bool | None = None
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": turn.id,
            "client_turn_id": turn.client_turn_id,
            "role": turn.role,
            "request_content": turn.request_content,
            "text": turn.content,
            "blocks": tuple(turn.blocks),
            "created_at": turn.created_at,
        }
        if replayed is not None:
            payload["replayed"] = replayed
        return payload

    @staticmethod
    def _owned(
        db: Session,
        owner_user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> AssistantConversation:
        conversation = db.scalar(
            select(AssistantConversation).where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.owner_user_id == owner_user_id,
                AssistantConversation.module_key == MODULE_KEY,
                AssistantConversation.is_archived.is_(False),
            )
        )
        if conversation is None:
            raise ToolError("procurement_conversation_not_found")
        return conversation


__all__ = ["MODULE_KEY", "ProcurementConversationStore"]
