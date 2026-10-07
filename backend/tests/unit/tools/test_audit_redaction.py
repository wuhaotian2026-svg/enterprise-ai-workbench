from __future__ import annotations

from uuid import uuid4

from policy_api.tools.audit import AuditSummary, hash_sensitive_text


def test_audit_summary_hashes_reason_and_accepts_only_documented_fields() -> None:
    secret = "secret-reason-do-not-log"
    operation_id = uuid4()

    summary = AuditSummary.for_execution(
        tool_name="hr.submit_leave_request",
        risk_level="write",
        outcome="failed",
        operation_id=operation_id,
        duration_ms=42,
        reason=secret,
    )
    payload = summary.payload()

    assert payload == {
        "tool_name": "hr.submit_leave_request",
        "risk_level": "write",
        "outcome": "failed",
        "operation_id": str(operation_id),
        "duration_ms": 42,
        "reason_hash": hash_sensitive_text(secret),
    }
    assert secret not in repr(summary)
    assert secret not in repr(payload)


def test_sensitive_hash_is_deterministic_without_echoing_input() -> None:
    secret = "secret-reason-do-not-log"

    first = hash_sensitive_text(secret)
    second = hash_sensitive_text(secret)

    assert first == second
    assert len(first) == 64
    assert secret not in first
