from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from policy_api.slot_extraction.errors import SlotExtractionError
from policy_api.slot_extraction.fingerprint import (
    RequestFingerprint,
    SlotExtractionRequestIdentity,
)
from policy_api.slot_extraction.models import (
    SlotExtractionOperation,
    SlotExtractionOperationStatus,
)


@dataclass(frozen=True, slots=True)
class SlotExtractionClaim:
    operation: SlotExtractionOperation
    claimed: bool


class SlotExtractionOperationRepository:
    """Coordinates extraction ledger rows without owning transaction boundaries."""

    def claim(
        self,
        db: Session,
        request: SlotExtractionRequestIdentity,
        fingerprint: RequestFingerprint,
        *,
        model_name: str,
        now: datetime,
        timeout_seconds: int,
    ) -> SlotExtractionClaim:
        operation_id = uuid.uuid4()
        claimed_id = db.scalar(
            insert(SlotExtractionOperation)
            .values(
                id=operation_id,
                owner_user_id=request.owner_user_id,
                module_key=request.module_key,
                conversation_id=request.conversation_id,
                client_turn_id=request.client_turn_id,
                request_fingerprint=fingerprint.value,
                fingerprint_key_id=fingerprint.key_id,
                slot_schema_version=request.slot_schema_version,
                slot_schema_sha256=request.slot_schema_sha256,
                model_name=model_name,
                status=SlotExtractionOperationStatus.RESERVED,
                deadline_at=now + timedelta(seconds=timeout_seconds),
                dispatched_at=None,
                completed_at=None,
                accepted_count=0,
                pending_count=0,
                rejected_count=0,
                error_code=None,
                latency_ms=None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["owner_user_id", "module_key", "client_turn_id"]
            )
            .returning(SlotExtractionOperation.id)
        )
        if claimed_id is not None:
            operation = self._get_by_id(db, claimed_id)
        else:
            operation = db.scalar(
                select(SlotExtractionOperation)
                .where(
                    SlotExtractionOperation.owner_user_id == request.owner_user_id,
                    SlotExtractionOperation.module_key == request.module_key,
                    SlotExtractionOperation.client_turn_id == request.client_turn_id,
                )
                .execution_options(populate_existing=True)
            )
        if operation is None:
            raise SlotExtractionError("slot_extraction_operation_claim_failed")
        if operation.fingerprint_key_id != fingerprint.key_id:
            raise SlotExtractionError("slot_extraction_fingerprint_key_changed")
        if not hmac.compare_digest(
            operation.request_fingerprint,
            fingerprint.value,
        ):
            raise SlotExtractionError("client_turn_id_conflict")
        return SlotExtractionClaim(
            operation=operation,
            claimed=claimed_id is not None,
        )

    def mark_dispatched(
        self,
        db: Session,
        operation_id: uuid.UUID,
        *,
        now: datetime,
        timeout_seconds: int,
    ) -> SlotExtractionOperation:
        result = db.execute(
            update(SlotExtractionOperation)
            .where(
                SlotExtractionOperation.id == operation_id,
                SlotExtractionOperation.status
                == SlotExtractionOperationStatus.RESERVED,
            )
            .values(
                status=SlotExtractionOperationStatus.DISPATCHED,
                dispatched_at=now,
                deadline_at=now + timedelta(seconds=timeout_seconds),
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            self._raise_transition_error(db, operation_id)
        return self._require_by_id(db, operation_id)

    def converge_stale(
        self,
        db: Session,
        *,
        now: datetime,
        operation_id: uuid.UUID | None = None,
    ) -> int:
        statement = update(SlotExtractionOperation).where(
            SlotExtractionOperation.status.in_(
                (
                    SlotExtractionOperationStatus.RESERVED,
                    SlotExtractionOperationStatus.DISPATCHED,
                )
            ),
            SlotExtractionOperation.deadline_at <= now,
        )
        if operation_id is not None:
            statement = statement.where(SlotExtractionOperation.id == operation_id)
        result = db.execute(
            statement.values(
                status=SlotExtractionOperationStatus.INDETERMINATE,
                completed_at=now,
                error_code="slot_extraction_outcome_indeterminate",
                updated_at=now,
            )
        )
        return result.rowcount

    def complete_success(
        self,
        db: Session,
        operation_id: uuid.UUID,
        *,
        accepted: int,
        pending: int,
        rejected: int,
        now: datetime,
    ) -> SlotExtractionOperation:
        result = db.execute(
            update(SlotExtractionOperation)
            .where(
                SlotExtractionOperation.id == operation_id,
                SlotExtractionOperation.status
                == SlotExtractionOperationStatus.DISPATCHED,
                SlotExtractionOperation.deadline_at > now,
            )
            .values(
                status=SlotExtractionOperationStatus.SUCCEEDED,
                completed_at=now,
                accepted_count=accepted,
                pending_count=pending,
                rejected_count=rejected,
                error_code=None,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            self.converge_stale(db, now=now, operation_id=operation_id)
            self._raise_transition_error(db, operation_id)
        return self._require_by_id(db, operation_id)

    def complete_failure(
        self,
        db: Session,
        operation_id: uuid.UUID,
        *,
        error_code: str,
        latency_ms: int | None,
        now: datetime,
    ) -> SlotExtractionOperation:
        result = db.execute(
            update(SlotExtractionOperation)
            .where(
                SlotExtractionOperation.id == operation_id,
                SlotExtractionOperation.status
                == SlotExtractionOperationStatus.DISPATCHED,
            )
            .values(
                status=SlotExtractionOperationStatus.FAILED,
                completed_at=now,
                error_code=error_code,
                latency_ms=latency_ms,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            self._raise_transition_error(db, operation_id)
        return self._require_by_id(db, operation_id)

    @staticmethod
    def _get_by_id(
        db: Session,
        operation_id: uuid.UUID,
    ) -> SlotExtractionOperation | None:
        return db.scalar(
            select(SlotExtractionOperation)
            .where(SlotExtractionOperation.id == operation_id)
            .execution_options(populate_existing=True)
        )

    def _require_by_id(
        self,
        db: Session,
        operation_id: uuid.UUID,
    ) -> SlotExtractionOperation:
        operation = self._get_by_id(db, operation_id)
        if operation is None:
            raise SlotExtractionError("slot_extraction_operation_not_found")
        return operation

    def _raise_transition_error(
        self,
        db: Session,
        operation_id: uuid.UUID,
    ) -> None:
        operation = self._get_by_id(db, operation_id)
        if (
            operation is not None
            and operation.status == SlotExtractionOperationStatus.INDETERMINATE
        ):
            raise SlotExtractionError("slot_extraction_outcome_indeterminate")
        raise SlotExtractionError("slot_extraction_transition_invalid")


__all__ = ["SlotExtractionClaim", "SlotExtractionOperationRepository"]
