from __future__ import annotations

from dataclasses import replace
import importlib

import pytest
from pydantic import ValidationError


def _schemas():
    return importlib.import_module("policy_api.slot_extraction.schemas")


def _errors():
    return importlib.import_module("policy_api.slot_extraction.errors")


def _module_schema(*, repeatable: bool = False, max_candidates: int = 3):
    schemas = _schemas()
    return schemas.ModuleSlotSchema.build(
        module_key="hr",
        version="slot-extraction-v1",
        definitions=(
            schemas.ModuleSlotDefinition(
                name="reason",
                raw_kind="scalar",
                repeatable=repeatable,
                max_candidates=max_candidates,
            ),
        ),
    )


def _item_module_schema():
    return _item_module_schema_with_raw_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "item_name": {"type": "string"},
            },
        }
    )


def _item_module_schema_with_raw_schema(raw_schema: dict[str, object] | None):
    schemas = _schemas()
    return schemas.ModuleSlotSchema.build(
        module_key="procurement",
        version="slot-extraction-v1",
        definitions=(
            schemas.ModuleSlotDefinition(
                name="items",
                raw_kind="item",
                repeatable=True,
                max_candidates=50,
                raw_schema=raw_schema,
            ),
        ),
    )


def _item_envelope(raw_value: dict[str, str | None]):
    schemas = _schemas()
    return schemas.SlotExtractionEnvelope.model_validate(
        {
            "schema_version": "slot-extraction-v1",
            "candidates": [
                {
                    "slot_name": "items",
                    "raw_value": raw_value,
                    "source_quote": "采购桌椅",
                }
            ],
        }
    )


def test_candidate_envelope_is_closed_and_bounded() -> None:
    schemas = _schemas()
    envelope = schemas.SlotExtractionEnvelope.model_validate(
        {
            "schema_version": "slot-extraction-v1",
            "candidates": [
                {
                    "slot_name": "reason",
                    "raw_value": "探亲",
                    "source_quote": "用于探亲",
                }
            ],
        }
    )
    assert envelope.candidates[0].source_quote == "用于探亲"

    with pytest.raises(ValidationError):
        schemas.SlotExtractionEnvelope.model_validate(
            {
                "schema_version": "slot-extraction-v1",
                "candidates": [],
                "confidence": 0.99,
            }
        )


def test_candidate_rejects_extra_fields_and_non_string_leaves() -> None:
    schemas = _schemas()
    with pytest.raises(ValidationError):
        schemas.SlotCandidate.model_validate(
            {
                "slot_name": "reason",
                "raw_value": "探亲",
                "source_quote": "探亲",
                "reasoning": "because",
            }
        )
    with pytest.raises(ValidationError):
        schemas.SlotCandidate.model_validate(
            {
                "slot_name": "items",
                "raw_value": {"quantity": 3},
                "source_quote": "三把椅子",
            }
        )


def test_unknown_slot_is_envelope_level_failure() -> None:
    schemas = _schemas()
    errors = _errors()
    envelope = schemas.SlotExtractionEnvelope.model_validate(
        {
            "schema_version": "slot-extraction-v1",
            "candidates": [
                {
                    "slot_name": "employee_id",
                    "raw_value": "123",
                    "source_quote": "员工123",
                }
            ],
        }
    )
    with pytest.raises(errors.SlotExtractionError, match="slot_extraction_schema_invalid"):
        schemas.validate_envelope_for_module(envelope, _module_schema())


def test_unknown_nested_item_leaf_is_envelope_level_failure() -> None:
    schemas = _schemas()
    errors = _errors()
    envelope = schemas.SlotExtractionEnvelope.model_validate(
        {
            "schema_version": "slot-extraction-v1",
            "candidates": [
                {
                    "slot_name": "items",
                    "raw_value": {
                        "item_name": "桌子",
                        "supplier_id": "external-supplier",
                    },
                    "source_quote": "桌子 external-supplier",
                }
            ],
        }
    )

    with pytest.raises(errors.SlotExtractionError, match="slot_extraction_schema_invalid"):
        schemas.validate_envelope_for_module(envelope, _item_module_schema())


def test_item_required_leaf_must_be_present_at_envelope_boundary() -> None:
    schemas = _schemas()
    errors = _errors()
    schema = _item_module_schema_with_raw_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["unit"],
            "properties": {
                "unit": {"type": ["string", "null"]},
            },
        }
    )

    with pytest.raises(errors.SlotExtractionError, match="slot_extraction_schema_invalid"):
        schemas.validate_envelope_for_module(_item_envelope({}), schema)


@pytest.mark.parametrize(
    "required",
    [
        "unit",
        ["unit", "unit"],
        ["undeclared"],
        [1],
    ],
)
def test_malformed_item_required_contract_fails_closed(required: object) -> None:
    schemas = _schemas()
    errors = _errors()
    schema = _item_module_schema_with_raw_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": {
                "unit": {"type": ["string", "null"]},
            },
        }
    )

    with pytest.raises(errors.SlotExtractionError, match="slot_extraction_schema_invalid"):
        schemas.validate_envelope_for_module(
            _item_envelope({"unit": None}),
            schema,
        )


@pytest.mark.parametrize(
    "property_schema",
    [
        {"type": "integer"},
        {"type": []},
        {"anyOf": []},
        {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        {"anyOf": [{"type": "string"}, "not-a-schema"]},
        {"oneOf": [{"type": "string"}]},
    ],
)
def test_unsupported_or_malformed_item_leaf_schema_fails_closed(
    property_schema: dict[str, object],
) -> None:
    schemas = _schemas()
    errors = _errors()
    schema = _item_module_schema_with_raw_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"item_name": property_schema},
        }
    )

    with pytest.raises(errors.SlotExtractionError, match="slot_extraction_schema_invalid"):
        schemas.validate_envelope_for_module(
            _item_envelope({"item_name": "桌子"}),
            schema,
        )


@pytest.mark.parametrize("additional_properties", [None, "invalid", 1])
def test_malformed_additional_properties_fails_closed(
    additional_properties: object,
) -> None:
    schemas = _schemas()
    errors = _errors()
    schema = _item_module_schema_with_raw_schema(
        {
            "type": "object",
            "additionalProperties": additional_properties,
            "properties": {"item_name": {"type": "string"}},
        }
    )

    with pytest.raises(errors.SlotExtractionError, match="slot_extraction_schema_invalid"):
        schemas.validate_envelope_for_module(
            _item_envelope({"item_name": "桌子"}),
            schema,
        )


def test_non_mapping_item_raw_schema_fails_closed() -> None:
    schemas = _schemas()
    errors = _errors()
    valid_schema = _item_module_schema()
    malformed_definition = replace(
        valid_schema.definitions[0],
        raw_schema=["type", "properties"],  # type: ignore[arg-type]
    )
    malformed_schema = replace(
        valid_schema,
        definitions=(malformed_definition,),
    )

    with pytest.raises(errors.SlotExtractionError, match="slot_extraction_schema_invalid"):
        schemas.validate_envelope_for_module(
            _item_envelope({"item_name": "桌子"}),
            malformed_schema,
        )


def test_item_type_list_supports_required_nullable_leaf() -> None:
    schemas = _schemas()
    schema = _item_module_schema_with_raw_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["unit"],
            "properties": {
                "unit": {"type": ["string", "null"]},
            },
        }
    )
    envelope = _item_envelope({"unit": None})

    assert schemas.validate_envelope_for_module(envelope, schema) is envelope


def test_item_without_custom_raw_schema_keeps_generic_mapping_compatibility() -> None:
    schemas = _schemas()
    schema = _item_module_schema_with_raw_schema(None)
    envelope = _item_envelope(
        {
            "legacy_leaf": "原始值",
            "nullable_leaf": None,
        }
    )

    assert schemas.validate_envelope_for_module(envelope, schema) is envelope


def test_schema_version_mismatch_is_fail_closed() -> None:
    schemas = _schemas()
    errors = _errors()
    envelope = schemas.SlotExtractionEnvelope.model_validate(
        {"schema_version": "slot-extraction-v2", "candidates": []}
    )
    with pytest.raises(errors.SlotExtractionError, match="slot_extraction_schema_invalid"):
        schemas.validate_envelope_for_module(envelope, _module_schema())


def test_nonrepeatable_slot_candidate_limit_is_enforced() -> None:
    schemas = _schemas()
    errors = _errors()
    envelope = schemas.SlotExtractionEnvelope.model_validate(
        {
            "schema_version": "slot-extraction-v1",
            "candidates": [
                {"slot_name": "reason", "raw_value": value, "source_quote": value}
                for value in ("甲", "乙", "丙", "丁")
            ],
        }
    )
    with pytest.raises(errors.SlotExtractionError, match="slot_extraction_schema_invalid"):
        schemas.validate_envelope_for_module(envelope, _module_schema())


def test_provider_schema_is_stable_and_hash_bound() -> None:
    first = _module_schema()
    second = _module_schema()
    assert first.provider_json == second.provider_json
    assert first.sha256 == second.sha256
    assert len(first.sha256) == 64
    assert first.provider_json["additionalProperties"] is False


def test_provider_schema_exposes_slot_semantics_and_verbatim_source_contract() -> None:
    schemas = _schemas()
    schema = schemas.ModuleSlotSchema.build(
        module_key="hr",
        version="slot-extraction-v1",
        definitions=(
            schemas.ModuleSlotDefinition(
                name="reason",
                raw_kind="scalar",
                description="Explicit reason text from this user turn.",
            ),
        ),
    )

    variant = schema.provider_json["properties"]["candidates"]["items"]["oneOf"][0]
    assert variant["description"] == "Explicit reason text from this user turn."
    assert "verbatim" in variant["properties"]["raw_value"]["description"]
    assert "current_user_turn_text" in variant["properties"]["source_quote"]["description"]
