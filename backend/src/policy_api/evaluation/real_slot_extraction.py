"""Safe real-model runner for current-turn Candidate Slot Extraction."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from policy_api.evaluation.slot_extraction import (
    SlotExtractionCase,
    SlotExtractionExpectation,
    SlotExtractionInputError,
    SlotExtractionTrace,
    SourceEvidence,
    assess_slot_extraction_quality,
    load_slot_extraction_catalog,
    score_slot_extraction_results,
)
from policy_api.evaluation.tool_calling import _atomic_write_json
from policy_api.hr.draft import merge_hr_draft, project_hr_clarification
from policy_api.hr.slot_schema import HR_SLOT_SCHEMA
from policy_api.hr.slot_validation import validate_hr_candidates
from policy_api.procurement.draft import (
    merge_procurement_draft,
    project_procurement_clarification_fields,
)
from policy_api.procurement.draft_activation import (
    ProcurementDraftActivationPolicy,
    build_procurement_draft_activation_policy,
    has_procurement_draft_state,
    has_procurement_draftable_disposition,
)
from policy_api.procurement.slot_schema import PROCUREMENT_SLOT_SCHEMA
from policy_api.procurement.slot_validation import validate_procurement_candidates
from policy_api.procurement.tool_flow_policy import ProcurementToolFlowPolicy
from policy_api.slot_extraction.client import (
    SLOT_EXTRACTION_SYSTEM_MESSAGE,
    SlotExtractionClient,
)
from policy_api.slot_extraction.errors import SlotExtractionError
from policy_api.slot_extraction.fingerprint import (
    RequestFingerprint,
    SlotExtractionFingerprinter,
    SlotExtractionRequestIdentity,
)
from policy_api.slot_extraction.models import SlotExtractionOperationStatus
from policy_api.slot_extraction.repository import SlotExtractionClaim
from policy_api.slot_extraction.service import (
    SlotExtractionPreparationRequest,
    SlotExtractionService,
)
from policy_api.tools.probe_config import ProbeSettings


BACKEND_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CASES = BACKEND_ROOT / "evaluation" / "slot_extraction_cases.json"
DEFAULT_MANIFEST = BACKEND_ROOT / "evaluation" / "slot_extraction_manifest.json"
REFERENCE_DATE = "2026-08-28"
REFERENCE_TIMEZONE = "Asia/Shanghai"
TOOL_CALLING_LIMITS = {"model": 3, "read": 4, "write": 1}
EXTRACTION_CALL_LIMIT = 1
_EVALUATION_HMAC_MATERIAL = b"slot-extraction-evaluation-ledger-v1"


@dataclass(slots=True)
class _MemoryOperation:
    id: uuid.UUID
    request_fingerprint: str
    fingerprint_key_id: str
    status: SlotExtractionOperationStatus
    deadline_at: datetime
    error_code: str | None = None
    accepted_count: int = 0
    pending_count: int = 0
    rejected_count: int = 0
    latency_ms: int | None = None


class _MemoryOperationRepository:
    """Evaluation-only ledger adapter for the production coordination service."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[uuid.UUID, str, uuid.UUID], _MemoryOperation] = {}
        self._by_id: dict[uuid.UUID, _MemoryOperation] = {}

    def claim(
        self,
        _db: object,
        request: SlotExtractionRequestIdentity,
        fingerprint: RequestFingerprint,
        *,
        model_name: str,
        now: datetime,
        timeout_seconds: int,
    ) -> SlotExtractionClaim:
        del model_name
        key = (request.owner_user_id, request.module_key, request.client_turn_id)
        operation = self._by_key.get(key)
        if operation is not None:
            if operation.fingerprint_key_id != fingerprint.key_id:
                raise SlotExtractionError("slot_extraction_fingerprint_key_changed")
            if not hmac.compare_digest(
                operation.request_fingerprint, fingerprint.value
            ):
                raise SlotExtractionError("client_turn_id_conflict")
            return SlotExtractionClaim(operation=operation, claimed=False)  # type: ignore[arg-type]
        operation = _MemoryOperation(
            id=uuid.uuid4(),
            request_fingerprint=fingerprint.value,
            fingerprint_key_id=fingerprint.key_id,
            status=SlotExtractionOperationStatus.RESERVED,
            deadline_at=now + timedelta(seconds=timeout_seconds),
        )
        self._by_key[key] = operation
        self._by_id[operation.id] = operation
        return SlotExtractionClaim(operation=operation, claimed=True)  # type: ignore[arg-type]

    def mark_dispatched(
        self,
        _db: object,
        operation_id: uuid.UUID,
        *,
        now: datetime,
        timeout_seconds: int,
    ) -> _MemoryOperation:
        operation = self._require(operation_id)
        if operation.status is not SlotExtractionOperationStatus.RESERVED:
            raise SlotExtractionError("slot_extraction_transition_invalid")
        operation.status = SlotExtractionOperationStatus.DISPATCHED
        operation.deadline_at = now + timedelta(seconds=timeout_seconds)
        return operation

    def converge_stale(
        self,
        _db: object,
        *,
        now: datetime,
        operation_id: uuid.UUID | None = None,
    ) -> int:
        operations = (
            [self._require(operation_id)]
            if operation_id is not None
            else list(self._by_id.values())
        )
        changed = 0
        for operation in operations:
            if (
                operation.status in {
                    SlotExtractionOperationStatus.RESERVED,
                    SlotExtractionOperationStatus.DISPATCHED,
                }
                and operation.deadline_at <= now
            ):
                operation.status = SlotExtractionOperationStatus.INDETERMINATE
                operation.error_code = "slot_extraction_outcome_indeterminate"
                changed += 1
        return changed

    def complete_success(
        self,
        _db: object,
        operation_id: uuid.UUID,
        *,
        accepted: int,
        pending: int,
        rejected: int,
        now: datetime,
    ) -> _MemoryOperation:
        operation = self._require(operation_id)
        if (
            operation.status is not SlotExtractionOperationStatus.DISPATCHED
            or operation.deadline_at <= now
        ):
            self.converge_stale(_db, now=now, operation_id=operation_id)
            raise SlotExtractionError("slot_extraction_outcome_indeterminate")
        operation.status = SlotExtractionOperationStatus.SUCCEEDED
        operation.accepted_count = accepted
        operation.pending_count = pending
        operation.rejected_count = rejected
        return operation

    def complete_failure(
        self,
        _db: object,
        operation_id: uuid.UUID,
        *,
        error_code: str,
        latency_ms: int | None,
        now: datetime,
    ) -> _MemoryOperation:
        del now
        operation = self._require(operation_id)
        if operation.status is not SlotExtractionOperationStatus.DISPATCHED:
            raise SlotExtractionError("slot_extraction_transition_invalid")
        operation.status = SlotExtractionOperationStatus.FAILED
        operation.error_code = error_code
        operation.latency_ms = latency_ms
        return operation

    def _require(self, operation_id: uuid.UUID) -> _MemoryOperation:
        operation = self._by_id.get(operation_id)
        if operation is None:
            raise SlotExtractionError("slot_extraction_operation_not_found")
        return operation


class _CommitOnlySession:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def _reason_map(payload: dict[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in payload.items():
        if isinstance(value, dict) and isinstance(value.get("reason_code"), str):
            result[name] = str(value["reason_code"])
    return result


class SafeRealSlotExtractionEvaluator:
    def __init__(
        self,
        *,
        client: object,
        reference_date: date = date.fromisoformat(REFERENCE_DATE),
        timeout_seconds: int = 30,
        draft_activation_policy: ProcurementDraftActivationPolicy | None = None,
    ) -> None:
        self.client = client
        self.reference_date = reference_date
        self.repository = _MemoryOperationRepository()
        self.session = _CommitOnlySession()
        self.draft_activation_policy = (
            draft_activation_policy
            or build_procurement_draft_activation_policy()
        )
        self.service = SlotExtractionService(
            client=client,
            repository=self.repository,  # type: ignore[arg-type]
            fingerprinter=SlotExtractionFingerprinter(_EVALUATION_HMAC_MATERIAL),
            model_timeout_seconds=timeout_seconds,
        )

    def evaluate(
        self,
        case: SlotExtractionCase,
        expectation: SlotExtractionExpectation,
    ) -> SlotExtractionTrace:
        if case.id != expectation.case_id or case.module != expectation.module:
            raise SlotExtractionInputError("slot_extraction_case_contract_mismatch")
        schema = HR_SLOT_SCHEMA if case.module == "hr" else PROCUREMENT_SLOT_SCHEMA
        source_turn_id = str(uuid5(NAMESPACE_URL, f"slot-source:{case.id}"))
        request = SlotExtractionPreparationRequest(
            owner_user_id=uuid5(NAMESPACE_URL, "slot-evaluation-owner"),
            module_key=case.module,
            conversation_id=uuid5(NAMESPACE_URL, f"slot-conversation:{case.id}"),
            client_turn_id=uuid5(NAMESPACE_URL, f"slot-client-turn:{case.id}"),
            current_user_turn_text=case.current_user_turn,
            slot_schema=schema,
        )
        started = time.perf_counter()
        try:
            if case.module == "hr":
                validator = lambda text, envelope: validate_hr_candidates(
                    text=text,
                    envelope=envelope,
                    today=self.reference_date,
                    source_turn_id=source_turn_id,
                )
            else:
                validator = lambda text, envelope: validate_procurement_candidates(
                    text=text,
                    envelope=envelope,
                    today=self.reference_date,
                    source_turn_id=source_turn_id,
                )
            prepared = self.service.prepare(self.session, request, validator)  # type: ignore[arg-type]
            self.service.complete_success(self.session, prepared)  # type: ignore[arg-type]
            self.session.commit()
            if case.replay_same_turn:
                try:
                    self.service.prepare(self.session, request, validator)  # type: ignore[arg-type]
                except SlotExtractionError as replay_error:
                    if replay_error.code != "slot_extraction_turn_already_completed":
                        raise
                else:
                    raise SlotExtractionError("slot_extraction_replay_dispatched")
        except SlotExtractionError as exc:
            total_latency = max(0, round((time.perf_counter() - started) * 1000))
            return SlotExtractionTrace(
                envelope_valid=False,
                accepted_fields={},
                pending={},
                rejected=(),
                canonical_fields=dict(case.initial_draft),
                clarification_fields=(),
                terminal="error",
                extraction_calls=exc.slot_extraction_calls,
                total_turn_latency_ms=total_latency,
                error_code=exc.code,
            )

        validation = prepared.dispositions
        activation_error_code: str | None = None
        procurement_draft_route: bool | None = None
        if case.module == "hr":
            merged = merge_hr_draft(
                fields=case.initial_draft,
                pending=case.initial_pending,
                sources=case.initial_sources,
                validation=validation,
                control=None,
                source_turn_id=source_turn_id,
            )
        else:
            merged = merge_procurement_draft(
                fields=case.initial_draft,
                pending=case.initial_pending,
                sources=case.initial_sources,
                validation=validation,
                control=None,
                source_turn_id=source_turn_id,
                today=self.reference_date,
            )
            current_intent = ProcurementToolFlowPolicy._classify_intent(
                case.current_user_turn
            )
            has_active_draft = has_procurement_draft_state(
                fields=case.initial_draft,
                pending=case.initial_pending,
            )
            activation = self.draft_activation_policy.decide(
                current_intent=current_intent,
                has_active_draft=has_active_draft,
                validation=validation,
            )
            draft_route = activation.effective_intent in {
                "draft_request",
                "submit_request",
            }
            procurement_draft_route = draft_route
            if draft_route and not activation.should_save:
                activation_error_code = "procurement_draft_activation_invalid"
            elif (
                current_intent == "unknown"
                and (
                    has_active_draft
                    or has_procurement_draftable_disposition(validation)
                )
                and not draft_route
            ):
                activation_error_code = "procurement_draft_not_activated"
        pending = _reason_map(merged.pending)
        rejected = tuple(
            (item.slot_name, item.reason_code) for item in validation.rejected
        )
        accepted = {
            name: item.canonical_value for name, item in validation.accepted.items()
        }
        clarification = (
            project_procurement_clarification_fields(
                missing_fields=merged.missing_fields,
                pending=pending,
            )
            if case.module == "procurement"
            else project_hr_clarification(
                missing_fields=merged.missing_fields,
                pending=pending,
            ).clarification_fields
        )
        if activation_error_code is not None:
            terminal = "error"
            clarification = ()
        elif case.module == "procurement" and procurement_draft_route is False:
            clarification = tuple(sorted(pending))
            if pending:
                terminal = "clarification"
            elif rejected and not accepted:
                terminal = "rejected"
            else:
                terminal = "accepted"
        elif pending or merged.missing_fields:
            terminal = "clarification"
        elif rejected and not accepted:
            terminal = "rejected"
        else:
            terminal = "accepted"
        evidence = tuple(
            SourceEvidence(
                slot_name=name,
                source_turn_id=item.provenance.source_turn_id,
                match_kind=item.provenance.match_kind,
                source_spans=item.provenance.source_spans,
                verified=True,
            )
            for name, item in validation.accepted.items()
        )
        total_latency = max(0, round((time.perf_counter() - started) * 1000))
        incomplete_latency = total_latency if clarification else 0
        return SlotExtractionTrace(
            envelope_valid=True,
            accepted_fields=accepted,
            pending=pending,
            rejected=rejected,
            canonical_fields=dict(merged.fields),
            clarification_fields=clarification,
            terminal=terminal,
            source_evidence=evidence,
            extraction_calls=prepared.slot_extraction_calls,
            extraction_latency_ms=prepared.latency_ms,
            incomplete_turn_latency_ms=incomplete_latency,
            total_turn_latency_ms=total_latency,
            error_code=activation_error_code,
        )


_IMPLEMENTATION_RELATIVE_PATHS = (
    "assistant_drafts/presentation.py",
    "slot_extraction/schemas.py",
    "slot_extraction/normalization.py",
    "slot_extraction/fingerprint.py",
    "slot_extraction/client.py",
    "slot_extraction/models.py",
    "slot_extraction/repository.py",
    "slot_extraction/controls.py",
    "slot_extraction/merge.py",
    "slot_extraction/service.py",
    "hr/slot_schema.py",
    "hr/slot_validation.py",
    "hr/draft.py",
    "procurement/slot_schema.py",
    "procurement/slot_validation.py",
    "procurement/draft.py",
    "procurement/draft_activation.py",
    "procurement/tool_flow_policy.py",
    "procurement/runtime.py",
    "evaluation/slot_extraction.py",
    "evaluation/real_slot_extraction.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_slot_extraction_fingerprint(
    *,
    cases_path: str | Path,
    manifest_path: str | Path,
    model: str,
    reference_date: str,
    timeout_seconds: int | float,
) -> dict[str, object]:
    package_root = Path(__file__).resolve().parents[1]
    implementation = [
        {
            "path": relative,
            "sha256": _sha256(package_root / relative),
        }
        for relative in _IMPLEMENTATION_RELATIVE_PATHS
    ]
    payload: dict[str, object] = {
        "dataset_sha256": _sha256(Path(cases_path)),
        "manifest_sha256": _sha256(Path(manifest_path)),
        "implementation": implementation,
        "hr_schema_version": HR_SLOT_SCHEMA.version,
        "hr_schema_sha256": HR_SLOT_SCHEMA.sha256,
        "procurement_schema_version": PROCUREMENT_SLOT_SCHEMA.version,
        "procurement_schema_sha256": PROCUREMENT_SLOT_SCHEMA.sha256,
        "system_prompt_sha256": hashlib.sha256(
            SLOT_EXTRACTION_SYSTEM_MESSAGE.encode("utf-8")
        ).hexdigest(),
        "model": model,
        "temperature": 0,
        "thinking": "disabled",
        "provider_retry": 0,
        "reference_date": reference_date,
        "reference_timezone": REFERENCE_TIMEZONE,
        "timeout_seconds": timeout_seconds,
        "extraction_calls_per_new_turn": EXTRACTION_CALL_LIMIT,
        "tool_calling_limits": TOOL_CALLING_LIMITS,
    }
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return {
        **payload,
        "fingerprint_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def run_safe_real_evaluation(
    *,
    client: object,
    cases_path: str | Path,
    manifest_path: str | Path,
    output_path: str | Path,
    reference_date: str,
    timeout_seconds: int | float,
) -> dict[str, object]:
    cases, manifest = load_slot_extraction_catalog(cases_path, manifest_path)
    model = str(getattr(client, "model", ""))
    fingerprint = build_slot_extraction_fingerprint(
        cases_path=cases_path,
        manifest_path=manifest_path,
        model=model,
        reference_date=reference_date,
        timeout_seconds=timeout_seconds,
    )
    evaluator = SafeRealSlotExtractionEvaluator(
        client=client,
        reference_date=date.fromisoformat(reference_date),
        timeout_seconds=int(timeout_seconds),
    )
    traces = {
        case.id: evaluator.evaluate(case, manifest[case.id]) for case in cases
    }
    report = score_slot_extraction_results(cases, manifest, traces)
    report["configuration"] = {
        "mode": "slot_extraction_real_model_v1",
        "model": model,
        "reference_date": reference_date,
        "reference_timezone": REFERENCE_TIMEZONE,
        "temperature": 0,
        "thinking": "disabled",
        "provider_retry": 0,
        "timeout_seconds": timeout_seconds,
    }
    report["fingerprint"] = fingerprint
    report["quality_gate"] = assess_slot_extraction_quality(report)
    _atomic_write_json(Path(output_path), report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the safe real-model Candidate Slot Extraction suite."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-date", default=REFERENCE_DATE)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        settings = ProbeSettings(_env_file=None)
        date.fromisoformat(args.reference_date)
        if args.output.exists():
            raise SlotExtractionInputError("slot_extraction_output_exists")
        with SlotExtractionClient(
            base_url=str(settings.model_base_url),
            api_key=settings.model_api_key.get_secret_value(),
            model=settings.chat_model,
            timeout=settings.model_timeout_seconds,
        ) as client:
            report = run_safe_real_evaluation(
                client=client,
                cases_path=args.cases,
                manifest_path=args.manifest,
                output_path=args.output,
                reference_date=args.reference_date,
                timeout_seconds=settings.model_timeout_seconds,
            )
    except ValidationError:
        print("evaluation_error=provider_configuration_missing", file=sys.stderr)
        return 2
    except (OSError, ValueError, SlotExtractionInputError) as exc:
        print(f"evaluation_error={exc}", file=sys.stderr)
        return 1
    quality = report["quality_gate"]
    status = quality.get("status") if isinstance(quality, dict) else "failed"
    print(
        f"evaluation_status={status} "
        f"sample_count={report['metrics']['sample_count']} "
        f"extraction_p95_ms={report['metrics']['extraction_latency_p95_ms']}"
    )
    return 0 if status == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SafeRealSlotExtractionEvaluator",
    "build_slot_extraction_fingerprint",
    "run_safe_real_evaluation",
]
