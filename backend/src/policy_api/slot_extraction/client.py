from __future__ import annotations

import json

import httpx

from policy_api.slot_extraction.errors import SlotExtractionError
from policy_api.slot_extraction.schemas import (
    ModuleSlotSchema,
    SlotExtractionEnvelope,
    validate_envelope_for_module,
)


SLOT_EXTRACTION_SYSTEM_MESSAGE = (
    "Extract candidate slot values only from current_user_turn_text according to "
    "module_slot_schema. Return exactly one JSON object conforming to the supplied "
    "schema. Every candidate must include a source_quote verifiable in the current "
    "user turn. Copy source_quote as one exact contiguous substring. Do not "
    "canonicalize, translate, calculate, or infer raw_value: every non-null string "
    "leaf must be copied verbatim from its source_quote, apart from surrounding "
    "whitespace. raw_value contains only the semantic field value: exclude field "
    "labels or introducers such as title, purpose, reason, date, 标题, 用途, 原因, "
    "理由, or 用于. Choose a source_quote that is unique within current_user_turn_text; "
    "when the raw value repeats, include its nearest explicit field label in the "
    "quote while keeping that label out of raw_value. Follow each slot description "
    "and split compound item facts into their exact leaves; omit missing optional "
    "leaves. Within nested raw_value objects, emit JSON null for a property only when "
    "that property is listed in that object's required array and its schema "
    "explicitly allows null. If an optional property has no explicit value in "
    "current_user_turn_text, omit the property entirely; never emit JSON null as "
    "its placeholder. Numeric item leaves are JSON strings, never JSON numbers. Do "
    "not emit overlapping "
    "candidates for the same fact when the schema defines a preferred combined "
    "form. Explicit corrections and follow-up values must still be extracted. Do "
    "not explain or add unsupported fields. Return an empty candidates array only "
    "when no supported value is explicitly present."
)

MAX_PROVIDER_CONTENT_BYTES = 32 * 1024


def stable_current_turn_request_json(
    current_user_turn_text: str,
    slot_schema: ModuleSlotSchema,
) -> str:
    return json.dumps(
        {
            "current_user_turn_text": current_user_turn_text,
            "module_slot_schema": slot_schema.provider_json,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class SlotExtractionClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30,
    ) -> None:
        self.model = model
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def __enter__(self) -> SlotExtractionClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def extract(
        self,
        *,
        current_user_turn_text: str,
        slot_schema: ModuleSlotSchema,
    ) -> SlotExtractionEnvelope:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SLOT_EXTRACTION_SYSTEM_MESSAGE},
                {
                    "role": "user",
                    "content": stable_current_turn_request_json(
                        current_user_turn_text,
                        slot_schema,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0,
        }
        try:
            response = self.client.post("chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise SlotExtractionError(
                "slot_extraction_timeout",
                retryable=True,
            ) from exc

        if response.status_code == 429:
            raise SlotExtractionError(
                "slot_extraction_rate_limited",
                retryable=True,
                status_code=429,
            )
        if response.status_code >= 400:
            raise SlotExtractionError(
                "slot_extraction_provider_unavailable",
                retryable=response.status_code >= 500,
                status_code=response.status_code,
            )

        try:
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") == "content_filter":
                raise SlotExtractionError("slot_extraction_content_filtered")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError
            if len(content.encode("utf-8")) > MAX_PROVIDER_CONTENT_BYTES:
                raise ValueError
            envelope = SlotExtractionEnvelope.model_validate(json.loads(content))
            return validate_envelope_for_module(envelope, slot_schema)
        except SlotExtractionError:
            raise
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SlotExtractionError("slot_extraction_output_invalid") from exc
