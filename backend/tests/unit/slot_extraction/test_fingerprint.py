from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import replace
from uuid import UUID

import pytest


def _fingerprint():
    return importlib.import_module("policy_api.slot_extraction.fingerprint")


def _request(*, text: str = "用于探亲"):
    module = _fingerprint()
    return module.SlotExtractionRequestIdentity(
        owner_user_id=UUID("10000000-0000-0000-0000-000000000001"),
        module_key="hr",
        conversation_id=UUID("20000000-0000-0000-0000-000000000002"),
        client_turn_id=UUID("30000000-0000-0000-0000-000000000003"),
        slot_schema_version="slot-extraction-v1",
        slot_schema_sha256="a" * 64,
        current_user_turn_text=text,
    )


def test_request_fingerprint_is_keyed_deterministic_and_domain_separated() -> None:
    module = _fingerprint()
    request = _request()
    first = module.SlotExtractionFingerprinter(b"a" * 32).fingerprint(request)
    second = module.SlotExtractionFingerprinter(b"a" * 32).fingerprint(request)
    other_key = module.SlotExtractionFingerprinter(b"b" * 32).fingerprint(request)
    ordinary = hashlib.sha256(module.canonical_request_bytes(request)).hexdigest()
    assert first.value == second.value
    assert first.value != other_key.value
    assert first.value != ordinary
    assert first.key_id == "session-secret-hmac-sha256-v1"
    assert len(first.value) == 64
    assert first.value == first.value.lower()


def test_fingerprint_changes_for_every_scoped_component() -> None:
    module = _fingerprint()
    baseline = _request()
    changed = (
        replace(baseline, owner_user_id=UUID("10000000-0000-0000-0000-000000000009")),
        replace(baseline, module_key="procurement"),
        replace(baseline, conversation_id=UUID("20000000-0000-0000-0000-000000000009")),
        replace(baseline, client_turn_id=UUID("30000000-0000-0000-0000-000000000009")),
        replace(baseline, slot_schema_version="slot-extraction-v2"),
        replace(baseline, slot_schema_sha256="b" * 64),
        replace(baseline, current_user_turn_text="用于出差"),
    )
    fingerprinter = module.SlotExtractionFingerprinter(b"a" * 32)
    expected = fingerprinter.fingerprint(baseline)
    assert all(fingerprinter.fingerprint(item) != expected for item in changed)


def test_canonical_request_bytes_are_stable_compact_utf8_json() -> None:
    module = _fingerprint()
    encoded = module.canonical_request_bytes(_request())
    decoded = encoded.decode("utf-8")
    assert "用于探亲" in decoded
    assert " " not in decoded
    payload = json.loads(decoded)
    assert list(payload) == sorted(payload)
    assert payload["module_key"] == "hr"


def test_fingerprint_compare_uses_key_id_and_constant_time_value_comparison(monkeypatch) -> None:
    module = _fingerprint()
    calls: list[tuple[str, str]] = []

    def recording_compare(left: str, right: str) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr(module.hmac, "compare_digest", recording_compare)
    fingerprint = module.SlotExtractionFingerprinter(b"a" * 32).fingerprint(_request())
    assert module.fingerprint_matches(fingerprint, fingerprint.key_id, fingerprint.value)
    assert calls == [(fingerprint.value, fingerprint.value)]
    assert not module.fingerprint_matches(fingerprint, "another-key", fingerprint.value)
    assert calls == [(fingerprint.value, fingerprint.value)]


def test_fingerprint_repr_never_contains_key_text_or_canonical_bytes() -> None:
    module = _fingerprint()
    service = module.SlotExtractionFingerprinter(b"top-secret-value-that-is-long-enough")
    result = service.fingerprint(_request(text="用于探亲"))
    assert "top-secret-value" not in repr(service)
    assert "用于探亲" not in repr(service)
    assert "用于探亲" not in repr(result)
    assert "current_user_turn_text" not in repr(result)


def test_short_root_secret_is_rejected() -> None:
    module = _fingerprint()
    with pytest.raises(ValueError, match="slot_extraction_fingerprint_key_too_short"):
        module.SlotExtractionFingerprinter(b"short")
