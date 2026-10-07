from __future__ import annotations

import json
import logging

import pytest

from policy_api.observability import InvalidRequestId, log_event, normalize_request_id


def test_request_id_accepts_bounded_printable_client_value() -> None:
    assert normalize_request_id("trace-20260816-A") == "trace-20260816-A"


@pytest.mark.parametrize(
    "value",
    ["x" * 121, "line\nbreak", "carriage\rreturn", "zero\x00byte"],
)
def test_request_id_rejects_overlong_or_control_characters(value: str) -> None:
    with pytest.raises(InvalidRequestId):
        normalize_request_id(value)


def test_structured_log_keeps_operational_fields_and_redacts_sensitive_content(caplog) -> None:
    caplog.set_level(logging.INFO, logger="policy_api")
    log_event("ingestion_stage", request_id="req-1", resource_id="doc-7", stage="embedding",
        duration_ms=42, error_code="model_timeout", tool_name="hr.submit_leave_request",
        risk_level="write", operation_id="op-1", outcome="failed", password="pw-secret",
        cookie="session-secret", authorization="Bearer auth-secret", api_key="api-secret",
        policy_text="完整制度片段不得进入日志", reason="secret-reason-do-not-log",
        message="secret-message", arguments={"secret": True})
    payload = json.loads(caplog.records[-1].message)
    serialized = caplog.records[-1].message
    assert payload == {"event":"ingestion_stage", "request_id":"req-1", "resource_id":"doc-7",
        "stage":"embedding", "duration_ms":42, "error_code":"model_timeout",
        "tool_name":"hr.submit_leave_request", "risk_level":"write",
        "operation_id":"op-1", "outcome":"failed"}
    for secret in ("pw-secret", "session-secret", "auth-secret", "api-secret", "完整制度片段",
                   "secret-reason-do-not-log", "secret-message", "secret"):
        assert secret not in serialized
