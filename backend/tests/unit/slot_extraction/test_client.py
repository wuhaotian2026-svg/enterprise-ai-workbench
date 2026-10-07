from __future__ import annotations

import importlib
import json

import httpx
import pytest

from policy_api.procurement.slot_schema import PROCUREMENT_SLOT_SCHEMA
from policy_api.slot_extraction.schemas import ModuleSlotDefinition, ModuleSlotSchema


def _client_module():
    return importlib.import_module("policy_api.slot_extraction.client")


def _schema() -> ModuleSlotSchema:
    return ModuleSlotSchema.build(
        module_key="hr",
        version="slot-extraction-v1",
        definitions=(
            ModuleSlotDefinition(
                name="reason",
                raw_kind="scalar",
                description="Reason stated in the current user turn.",
            ),
        ),
    )


def _provider_response(
    envelope: dict[str, object],
    *,
    finish_reason: str = "stop",
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(envelope, ensure_ascii=False),
                    },
                }
            ]
        },
    )


def _valid_envelope() -> dict[str, object]:
    return {
        "schema_version": "slot-extraction-v1",
        "candidates": [
            {"slot_name": "reason", "raw_value": "探亲", "source_quote": "用于探亲"}
        ],
    }


def _procurement_item_envelope(
    raw_value: dict[str, str | None],
) -> dict[str, object]:
    return {
        "schema_version": PROCUREMENT_SLOT_SCHEMA.version,
        "candidates": [
            {
                "slot_name": "items",
                "raw_value": raw_value,
                "source_quote": "采购桌椅",
            }
        ],
    }


class CountingTransport(httpx.BaseTransport):
    def __init__(self, response_or_error: httpx.Response | Exception) -> None:
        self.response_or_error = response_or_error
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if isinstance(self.response_or_error, Exception):
            raise self.response_or_error
        return self.response_or_error


def _build_client(transport: httpx.BaseTransport):
    module = _client_module()
    return module.SlotExtractionClient(
        base_url="https://models.example.test/v1",
        api_key="secret",
        model="deepseek-v4-flash",
        transport=transport,
        timeout=3,
    )


def test_client_sends_only_fixed_protocol_current_text_and_slot_schema() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer secret"
        return _provider_response(_valid_envelope())

    with _build_client(httpx.MockTransport(handler)) as client:
        result = client.extract(
            current_user_turn_text="年份2026 用于探亲",
            slot_schema=_schema(),
        )

    assert result.candidates[0].raw_value == "探亲"
    assert captured["model"] == "deepseek-v4-flash"
    assert captured["temperature"] == 0
    assert captured["thinking"] == {"type": "disabled"}
    assert captured["response_format"] == {"type": "json_object"}
    assert len(captured["messages"]) == 2
    messages = captured["messages"]
    assert messages[0] == {
        "role": "system",
        "content": _client_module().SLOT_EXTRACTION_SYSTEM_MESSAGE,
    }
    user_payload = json.loads(messages[1]["content"])
    assert user_payload == {
        "current_user_turn_text": "年份2026 用于探亲",
        "module_slot_schema": _schema().provider_json,
    }


def test_system_message_requires_verbatim_non_overlapping_followup_extraction() -> None:
    message = _client_module().SLOT_EXTRACTION_SYSTEM_MESSAGE

    assert "Do not canonicalize" in message
    assert "verbatim" in message
    assert "Do not emit overlapping candidates" in message
    assert "Explicit corrections and follow-up values" in message
    assert "omit missing optional leaves" in message
    assert "field labels or introducers" in message
    assert "unique within current_user_turn_text" in message
    assert "JSON strings, never JSON numbers" in message


def test_system_message_defines_nested_null_and_optional_property_contract() -> None:
    message = _client_module().SLOT_EXTRACTION_SYSTEM_MESSAGE

    assert "only from current_user_turn_text" in message
    assert "source_quote verifiable in the current user turn" in message
    assert (
        "Within nested raw_value objects, emit JSON null for a property only when "
        "that property is listed in that object's required array and its schema "
        "explicitly allows null."
    ) in message
    assert (
        "If an optional property has no explicit value in current_user_turn_text, "
        "omit the property entirely; never emit JSON null as its placeholder."
    ) in message


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(429), "slot_extraction_rate_limited"),
        (httpx.Response(500), "slot_extraction_provider_unavailable"),
        (httpx.Response(503), "slot_extraction_provider_unavailable"),
    ],
)
def test_client_maps_provider_errors_without_retry(
    response: httpx.Response,
    code: str,
) -> None:
    transport = CountingTransport(response)
    with _build_client(transport) as client:
        with pytest.raises(_client_module().SlotExtractionError, match=code):
            client.extract(current_user_turn_text="文本", slot_schema=_schema())
    assert transport.calls == 1


def test_client_maps_timeout_without_retry() -> None:
    request = httpx.Request("POST", "https://models.example.test/v1/chat/completions")
    transport = CountingTransport(httpx.ReadTimeout("slow", request=request))
    with _build_client(transport) as client:
        with pytest.raises(
            _client_module().SlotExtractionError,
            match="slot_extraction_timeout",
        ):
            client.extract(current_user_turn_text="文本", slot_schema=_schema())
    assert transport.calls == 1


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            httpx.Response(200, json={"choices": []}),
            "slot_extraction_output_invalid",
        ),
        (
            httpx.Response(
                200,
                json={"choices": [{"finish_reason": "stop", "message": {"content": "not-json"}}]},
            ),
            "slot_extraction_output_invalid",
        ),
        (
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "```json\n{}\n```"},
                        }
                    ]
                },
            ),
            "slot_extraction_output_invalid",
        ),
        (
            _provider_response(
                {"schema_version": "slot-extraction-v1", "candidates": [], "extra": True}
            ),
            "slot_extraction_output_invalid",
        ),
        (
            _provider_response(_valid_envelope(), finish_reason="content_filter"),
            "slot_extraction_content_filtered",
        ),
    ],
)
def test_client_rejects_invalid_provider_outputs_without_retry(
    response: httpx.Response,
    code: str,
) -> None:
    transport = CountingTransport(response)
    with _build_client(transport) as client:
        with pytest.raises(_client_module().SlotExtractionError, match=code):
            client.extract(current_user_turn_text="文本", slot_schema=_schema())
    assert transport.calls == 1


@pytest.mark.parametrize(
    "raw_value",
    [
        {"category_hint": None},
        {"unit": None},
    ],
)
def test_client_rejects_items_missing_required_nullable_control_leaves(
    raw_value: dict[str, str | None],
) -> None:
    transport = CountingTransport(
        _provider_response(_procurement_item_envelope(raw_value))
    )

    with _build_client(transport) as client:
        with pytest.raises(_client_module().SlotExtractionError) as exc_info:
            client.extract(
                current_user_turn_text="采购桌椅",
                slot_schema=PROCUREMENT_SLOT_SCHEMA,
            )

    assert exc_info.value.code == "slot_extraction_schema_invalid"
    assert transport.calls == 1


@pytest.mark.parametrize(
    "raw_value",
    [
        {"unit": None, "category_hint": None},
        {
            "specification": None,
            "unit": None,
            "category_hint": None,
        },
        {"item_name": None, "unit": None, "category_hint": None},
        {"quantity": None, "unit": None, "category_hint": None},
        {
            "estimated_unit_price": None,
            "unit": None,
            "category_hint": None,
        },
    ],
)
def test_client_accepts_nullable_item_boundaries_without_retry(
    raw_value: dict[str, str | None],
) -> None:
    transport = CountingTransport(
        _provider_response(_procurement_item_envelope(raw_value))
    )

    with _build_client(transport) as client:
        result = client.extract(
            current_user_turn_text="采购桌椅",
            slot_schema=PROCUREMENT_SLOT_SCHEMA,
        )

    assert result.candidates[0].raw_value == raw_value
    assert transport.calls == 1


def test_client_rejects_response_larger_than_32_kib_without_parsing() -> None:
    oversized = "x" * (32 * 1024 + 1)
    transport = CountingTransport(
        httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": oversized}}
                ]
            },
        )
    )
    with _build_client(transport) as client:
        with pytest.raises(
            _client_module().SlotExtractionError,
            match="slot_extraction_output_invalid",
        ):
            client.extract(current_user_turn_text="文本", slot_schema=_schema())
    assert transport.calls == 1


def test_client_error_repr_does_not_include_provider_body() -> None:
    secret_body = "provider-secret-body"
    transport = CountingTransport(httpx.Response(503, text=secret_body))
    with _build_client(transport) as client:
        with pytest.raises(_client_module().SlotExtractionError) as captured:
            client.extract(current_user_turn_text="文本", slot_schema=_schema())
    assert secret_body not in repr(captured.value)
