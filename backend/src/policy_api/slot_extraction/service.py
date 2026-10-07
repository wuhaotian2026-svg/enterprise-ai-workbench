from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from policy_api.slot_extraction.controls import validate_current_user_turn_text
from policy_api.slot_extraction.errors import SlotExtractionError
from policy_api.slot_extraction.fingerprint import (
    SlotExtractionFingerprinter,
    SlotExtractionRequestIdentity,
)
from policy_api.slot_extraction.merge import CandidateValidationResult
from policy_api.slot_extraction.models import SlotExtractionOperationStatus
from policy_api.slot_extraction.repository import SlotExtractionOperationRepository
from policy_api.slot_extraction.schemas import ModuleSlotSchema, SlotExtractionEnvelope


@dataclass(frozen=True, slots=True)
class SlotExtractionPreparationRequest:
    owner_user_id: uuid.UUID
    module_key: Literal["hr", "procurement"]
    conversation_id: uuid.UUID
    client_turn_id: uuid.UUID
    current_user_turn_text: str
    slot_schema: ModuleSlotSchema

    def identity(self) -> SlotExtractionRequestIdentity:
        return SlotExtractionRequestIdentity(
            owner_user_id=self.owner_user_id,
            module_key=self.module_key,
            conversation_id=self.conversation_id,
            client_turn_id=self.client_turn_id,
            slot_schema_version=self.slot_schema.version,
            slot_schema_sha256=self.slot_schema.sha256,
            current_user_turn_text=self.current_user_turn_text,
        )


@dataclass(frozen=True, slots=True)
class PreparedExtraction:
    operation_id: uuid.UUID
    dispositions: CandidateValidationResult
    slot_extraction_calls: Literal[1]
    latency_ms: int


CandidateValidator = Callable[
    [str, SlotExtractionEnvelope],
    CandidateValidationResult,
]


class SlotExtractionService:
    def __init__(
        self,
        *,
        client: object,
        repository: SlotExtractionOperationRepository,
        fingerprinter: SlotExtractionFingerprinter,
        model_timeout_seconds: float,
        now_provider: Callable[[], datetime] | None = None,
        monotonic_provider: Callable[[], float] | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self.fingerprinter = fingerprinter
        self.model_timeout_seconds = model_timeout_seconds
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_provider or time.monotonic

    def prepare(
        self,
        db: Session,
        request: SlotExtractionPreparationRequest,
        validator: CandidateValidator,
    ) -> PreparedExtraction:
        validate_current_user_turn_text(request.current_user_turn_text)
        if request.slot_schema.module_key != request.module_key:
            raise SlotExtractionError("slot_extraction_schema_invalid")

        identity = request.identity()
        fingerprint = self.fingerprinter.fingerprint(identity)
        now = self._now()
        claim = self.repository.claim(
            db,
            identity,
            fingerprint,
            model_name=str(getattr(self.client, "model")),
            now=now,
            timeout_seconds=self.model_timeout_seconds,
        )
        db.commit()
        if not claim.claimed:
            changed = self.repository.converge_stale(
                db,
                now=now,
                operation_id=claim.operation.id,
            )
            if changed:
                db.commit()
                raise SlotExtractionError("slot_extraction_outcome_indeterminate")
            self._raise_replay(claim.operation)

        self.repository.mark_dispatched(
            db,
            claim.operation.id,
            now=self._now(),
            timeout_seconds=self.model_timeout_seconds,
        )
        db.commit()

        started = self._monotonic()
        try:
            envelope = self.client.extract(
                current_user_turn_text=request.current_user_turn_text,
                slot_schema=request.slot_schema,
            )
        except SlotExtractionError as exc:
            latency_ms = self._elapsed_ms(started)
            self._persist_failure(db, claim.operation.id, exc.code, latency_ms)
            raise SlotExtractionError(
                exc.code,
                retryable=exc.retryable,
                status_code=exc.status_code,
                slot_extraction_calls=1,
            ) from exc
        except Exception as exc:
            latency_ms = self._elapsed_ms(started)
            code = "slot_extraction_provider_unavailable"
            self._persist_failure(db, claim.operation.id, code, latency_ms)
            raise SlotExtractionError(code, slot_extraction_calls=1) from exc
        latency_ms = self._elapsed_ms(started)

        try:
            dispositions = validator(request.current_user_turn_text, envelope)
            if not isinstance(dispositions, CandidateValidationResult):
                raise TypeError
        except SlotExtractionError as exc:
            self._persist_failure(db, claim.operation.id, exc.code, latency_ms)
            raise SlotExtractionError(
                exc.code,
                retryable=exc.retryable,
                status_code=exc.status_code,
                slot_extraction_calls=1,
            ) from exc
        except Exception as exc:
            code = "slot_extraction_validation_failed"
            self._persist_failure(db, claim.operation.id, code, latency_ms)
            raise SlotExtractionError(code, slot_extraction_calls=1) from exc

        return PreparedExtraction(
            operation_id=claim.operation.id,
            dispositions=dispositions,
            slot_extraction_calls=1,
            latency_ms=latency_ms,
        )

    def complete_success(
        self,
        db: Session,
        prepared: PreparedExtraction,
    ) -> None:
        self.repository.complete_success(
            db,
            prepared.operation_id,
            accepted=prepared.dispositions.accepted_count,
            pending=prepared.dispositions.pending_count,
            rejected=prepared.dispositions.rejected_count,
            now=self._now(),
        )

    def _persist_failure(
        self,
        db: Session,
        operation_id: uuid.UUID,
        error_code: str,
        latency_ms: int,
    ) -> None:
        self.repository.complete_failure(
            db,
            operation_id,
            error_code=error_code,
            latency_ms=latency_ms,
            now=self._now(),
        )
        db.commit()

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._monotonic() - started) * 1000))

    @staticmethod
    def _raise_replay(operation: object) -> None:
        raw_status = getattr(operation, "status", None)
        status = raw_status.value if hasattr(raw_status, "value") else raw_status
        if status == SlotExtractionOperationStatus.FAILED.value:
            raise SlotExtractionError(
                str(getattr(operation, "error_code", None) or "slot_extraction_failed")
            )
        if status == SlotExtractionOperationStatus.INDETERMINATE.value:
            raise SlotExtractionError("slot_extraction_outcome_indeterminate")
        if status == SlotExtractionOperationStatus.SUCCEEDED.value:
            raise SlotExtractionError("slot_extraction_turn_already_completed")
        raise SlotExtractionError("slot_extraction_in_progress")


__all__ = [
    "CandidateValidator",
    "PreparedExtraction",
    "SlotExtractionPreparationRequest",
    "SlotExtractionService",
]
