from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import importlib
from types import SimpleNamespace
import uuid

import pytest

from policy_api.slot_extraction.errors import SlotExtractionError
from policy_api.slot_extraction.fingerprint import SlotExtractionFingerprinter
from policy_api.slot_extraction.models import SlotExtractionOperationStatus
from policy_api.slot_extraction.schemas import (
    ModuleSlotDefinition,
    ModuleSlotSchema,
    SlotCandidate,
    SlotExtractionEnvelope,
)


NOW = datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc)


def _module():
    return importlib.import_module("policy_api.slot_extraction.service")


def _merge_module():
    return importlib.import_module("policy_api.slot_extraction.merge")


def _schema() -> ModuleSlotSchema:
    return ModuleSlotSchema.build(
        module_key="hr",
        version="slot-extraction-v1",
        definitions=(
            ModuleSlotDefinition(name="reason", raw_kind="scalar"),
        ),
    )


def _request():
    return _module().SlotExtractionPreparationRequest(
        owner_user_id=uuid.uuid4(),
        module_key="hr",
        conversation_id=uuid.uuid4(),
        client_turn_id=uuid.uuid4(),
        current_user_turn_text="用于探亲",
        slot_schema=_schema(),
    )


class FakeDb:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


class FakeClient:
    model = "deepseek-v4-flash"

    def __init__(self, error_code: str | None = None) -> None:
        self.error_code = error_code
        self.calls: list[tuple[str, ModuleSlotSchema]] = []

    def extract(
        self,
        *,
        current_user_turn_text: str,
        slot_schema: ModuleSlotSchema,
    ) -> SlotExtractionEnvelope:
        self.calls.append((current_user_turn_text, slot_schema))
        if self.error_code is not None:
            raise SlotExtractionError(self.error_code)
        return SlotExtractionEnvelope(
            schema_version=slot_schema.version,
            candidates=[
                SlotCandidate(
                    slot_name="reason",
                    raw_value="探亲",
                    source_quote="用于探亲",
                )
            ],
        )


class FakeRepository:
    def __init__(
        self,
        *,
        claimed: bool = True,
        status=None,
        error_code=None,
        stale_changed: bool = False,
    ) -> None:
        self.operation = SimpleNamespace(
            id=uuid.uuid4(),
            status=status or SlotExtractionOperationStatus.RESERVED,
            error_code=error_code,
        )
        self.claimed = claimed
        self.stale_changed = stale_changed
        self.claim_calls = []
        self.dispatched = []
        self.failures = []
        self.successes = []

    def claim(self, _db, request, fingerprint, **kwargs):
        self.claim_calls.append((request, fingerprint, kwargs))
        return SimpleNamespace(operation=self.operation, claimed=self.claimed)

    def mark_dispatched(self, _db, operation_id, **kwargs):
        self.dispatched.append((operation_id, kwargs))
        self.operation.status = SlotExtractionOperationStatus.DISPATCHED
        return self.operation

    def complete_failure(self, _db, operation_id, **kwargs):
        self.failures.append((operation_id, kwargs))
        self.operation.status = SlotExtractionOperationStatus.FAILED
        self.operation.error_code = kwargs["error_code"]
        return self.operation

    def complete_success(self, _db, operation_id, **kwargs):
        self.successes.append((operation_id, kwargs))
        self.operation.status = SlotExtractionOperationStatus.SUCCEEDED
        return self.operation

    def converge_stale(self, _db, **_kwargs):
        if self.stale_changed:
            self.operation.status = SlotExtractionOperationStatus.INDETERMINATE
            return 1
        return 0


def _validation():
    accepted = _merge_module().AcceptedCandidate(
        slot_name="reason",
        canonical_value="探亲",
        provenance=_merge_module().CandidateProvenance(
            source_turn_id="turn-1",
            source_kind="user_explicit",
            slot_schema_version="slot-extraction-v1",
            source_spans=((0, 4),),
            validator_version="validator-v1",
            validation_status="accepted",
            match_kind="original_exact",
        ),
    )
    return _merge_module().CandidateValidationResult(
        accepted={"reason": accepted},
        pending={},
        rejected=(),
    )


def _service(client: FakeClient, repository: FakeRepository):
    return _module().SlotExtractionService(
        client=client,
        repository=repository,
        fingerprinter=SlotExtractionFingerprinter(b"s" * 32),
        model_timeout_seconds=30,
        now_provider=lambda: NOW,
        monotonic_provider=lambda: 1.0,
    )


def test_service_dispatches_client_once_and_passes_only_current_text_schema() -> None:
    client = FakeClient()
    repository = FakeRepository()
    db = FakeDb()
    request = _request()
    validator_calls = []

    result = _service(client, repository).prepare(
        db,
        request,
        lambda text, envelope: validator_calls.append((text, envelope)) or _validation(),
    )

    assert result.slot_extraction_calls == 1
    assert client.calls == [(request.current_user_turn_text, request.slot_schema)]
    assert len(validator_calls) == 1
    assert db.commits == 2
    assert len(repository.dispatched) == 1
    assert repository.failures == []


@pytest.mark.parametrize(
    "code",
    [
        "slot_extraction_timeout",
        "slot_extraction_rate_limited",
        "slot_extraction_provider_unavailable",
        "slot_extraction_output_invalid",
    ],
)
def test_provider_failure_is_persisted_without_retry_or_validation(code: str) -> None:
    client = FakeClient(error_code=code)
    repository = FakeRepository()
    db = FakeDb()
    validator_calls = []

    with pytest.raises(SlotExtractionError, match=code) as raised:
        _service(client, repository).prepare(
            db,
            _request(),
            lambda text, envelope: validator_calls.append((text, envelope)),
        )

    assert raised.value.slot_extraction_calls == 1
    assert len(client.calls) == 1
    assert validator_calls == []
    assert len(repository.failures) == 1
    assert repository.failures[0][1]["error_code"] == code
    assert db.commits == 3


def test_prepare_does_not_complete_success_before_runtime_merge() -> None:
    client = FakeClient()
    repository = FakeRepository()
    db = FakeDb()
    service = _service(client, repository)
    prepared = service.prepare(db, _request(), lambda _text, _envelope: _validation())

    assert repository.successes == []
    service.complete_success(db, prepared)
    assert {
        key: repository.successes[0][1][key]
        for key in ("accepted", "pending", "rejected")
    } == {
        "accepted": 1,
        "pending": 0,
        "rejected": 0,
    }
    assert db.commits == 2


def test_validator_failure_after_dispatch_reports_one_extraction_call() -> None:
    client = FakeClient()
    repository = FakeRepository()
    db = FakeDb()

    def reject_candidate(_text, _envelope):  # type: ignore[no-untyped-def]
        raise SlotExtractionError("slot_extraction_candidate_invalid")

    with pytest.raises(
        SlotExtractionError,
        match="slot_extraction_candidate_invalid",
    ) as raised:
        _service(client, repository).prepare(db, _request(), reject_candidate)

    assert raised.value.slot_extraction_calls == 1
    assert len(client.calls) == 1
    assert len(repository.failures) == 1


def test_terminal_failed_replay_never_calls_client_again() -> None:
    client = FakeClient()
    repository = FakeRepository(
        claimed=False,
        status=SlotExtractionOperationStatus.FAILED,
        error_code="slot_extraction_timeout",
    )
    db = FakeDb()

    with pytest.raises(
        SlotExtractionError,
        match="slot_extraction_timeout",
    ) as raised:
        _service(client, repository).prepare(
            db,
            _request(),
            lambda _text, _envelope: _validation(),
        )

    assert raised.value.slot_extraction_calls == 0
    assert client.calls == []
    assert repository.dispatched == []
    assert db.commits == 1


def test_stale_replay_reports_zero_extraction_calls() -> None:
    client = FakeClient()
    repository = FakeRepository(
        claimed=False,
        status=SlotExtractionOperationStatus.RESERVED,
        stale_changed=True,
    )
    db = FakeDb()

    with pytest.raises(
        SlotExtractionError,
        match="slot_extraction_outcome_indeterminate",
    ) as raised:
        _service(client, repository).prepare(
            db,
            _request(),
            lambda _text, _envelope: _validation(),
        )

    assert raised.value.slot_extraction_calls == 0
    assert client.calls == []
    assert repository.dispatched == []


def test_preflight_failure_reports_zero_extraction_calls() -> None:
    client = FakeClient()
    repository = FakeRepository()
    db = FakeDb()
    request = replace(_request(), current_user_turn_text="   ")

    with pytest.raises(
        SlotExtractionError,
        match="slot_extraction_request_invalid",
    ) as raised:
        _service(client, repository).prepare(
            db,
            request,
            lambda _text, _envelope: _validation(),
        )

    assert raised.value.slot_extraction_calls == 0
    assert repository.claim_calls == []
    assert client.calls == []
