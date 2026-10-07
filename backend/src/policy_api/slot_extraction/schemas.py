from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, StringConstraints

from policy_api.slot_extraction.errors import SlotExtractionError


RawScalar = Annotated[StrictStr, StringConstraints(min_length=1, max_length=2000)]


class SlotCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    slot_name: str = Field(min_length=1, max_length=80)
    raw_value: RawScalar | dict[str, RawScalar | None]
    source_quote: str = Field(min_length=1, max_length=2000)


class SlotExtractionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str = Field(min_length=1, max_length=80)
    candidates: list[SlotCandidate] = Field(max_length=64)


@dataclass(frozen=True, slots=True)
class ModuleSlotDefinition:
    name: str
    raw_kind: Literal["scalar", "item"]
    repeatable: bool = False
    max_candidates: int = 3
    description: str = ""
    raw_schema: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 80:
            raise ValueError("slot_definition_name_invalid")
        if self.max_candidates < 1 or self.max_candidates > 50:
            raise ValueError("slot_definition_candidate_limit_invalid")


@dataclass(frozen=True, slots=True)
class ModuleSlotSchema:
    module_key: Literal["hr", "procurement"]
    version: str
    definitions: tuple[ModuleSlotDefinition, ...]
    provider_json: Mapping[str, object]
    sha256: str

    @classmethod
    def build(
        cls,
        *,
        module_key: Literal["hr", "procurement"],
        version: str,
        definitions: tuple[ModuleSlotDefinition, ...],
    ) -> ModuleSlotSchema:
        if not definitions or len({item.name for item in definitions}) != len(definitions):
            raise ValueError("slot_definitions_invalid")
        variants: list[dict[str, object]] = []
        for definition in definitions:
            if definition.raw_schema is not None:
                raw_schema = deepcopy(definition.raw_schema)
            elif definition.raw_kind == "scalar":
                raw_schema = {"type": "string", "minLength": 1, "maxLength": 2000}
            else:
                raw_schema = {
                    "type": "object",
                    "additionalProperties": {"type": ["string", "null"]},
                }
            raw_schema.setdefault(
                "description",
                "Copy the supported raw value verbatim from source_quote; do not canonicalize it.",
            )
            variants.append(
                {
                    "type": "object",
                    "description": definition.description,
                    "additionalProperties": False,
                    "required": ["slot_name", "raw_value", "source_quote"],
                    "properties": {
                        "slot_name": {
                            "const": definition.name,
                            "description": definition.description,
                        },
                        "raw_value": raw_schema,
                        "source_quote": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                            "description": (
                                "Copy one exact contiguous quote from current_user_turn_text "
                                "that contains every non-null raw_value string leaf."
                            ),
                        },
                    },
                }
            )
        provider_json: dict[str, object] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "candidates"],
            "properties": {
                "schema_version": {"const": version},
                "candidates": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {"oneOf": variants},
                },
            },
        }
        encoded = json.dumps(
            provider_json,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(
            module_key=module_key,
            version=version,
            definitions=definitions,
            provider_json=provider_json,
            sha256=hashlib.sha256(encoded).hexdigest(),
        )


_ITEM_SCHEMA_ROOT_KEYS = frozenset(
    {
        "type",
        "additionalProperties",
        "required",
        "properties",
        "description",
        "examples",
    }
)
_LEAF_TYPE_SCHEMA_KEYS = frozenset(
    {"type", "minLength", "maxLength", "description", "examples"}
)
_LEAF_ANY_OF_SCHEMA_KEYS = frozenset({"anyOf", "description", "examples"})
_SUPPORTED_LEAF_TYPES = frozenset({"string", "null"})


def _schema_invalid() -> SlotExtractionError:
    return SlotExtractionError("slot_extraction_schema_invalid")


def _raw_leaf_matches_schema(
    value: RawScalar | None,
    property_schema: object,
) -> bool:
    if not isinstance(property_schema, Mapping) or not all(
        isinstance(key, str) for key in property_schema
    ):
        raise _schema_invalid()

    schema_keys = set(property_schema)
    if "anyOf" in property_schema:
        if not schema_keys.issubset(_LEAF_ANY_OF_SCHEMA_KEYS):
            raise _schema_invalid()
        branches = property_schema["anyOf"]
        if not isinstance(branches, (list, tuple)) or not branches:
            raise _schema_invalid()
        branch_matches = [
            _raw_leaf_matches_schema(value, branch)
            for branch in branches
        ]
        return any(branch_matches)

    if "type" not in property_schema or not schema_keys.issubset(
        _LEAF_TYPE_SCHEMA_KEYS
    ):
        raise _schema_invalid()
    raw_types = property_schema["type"]
    if isinstance(raw_types, str):
        leaf_types = (raw_types,)
    elif isinstance(raw_types, (list, tuple)):
        leaf_types = tuple(raw_types)
    else:
        raise _schema_invalid()
    if (
        not leaf_types
        or not all(isinstance(item, str) for item in leaf_types)
        or len(set(leaf_types)) != len(leaf_types)
        or not set(leaf_types).issubset(_SUPPORTED_LEAF_TYPES)
    ):
        raise _schema_invalid()

    min_length: int | None = None
    max_length: int | None = None
    if "minLength" in property_schema:
        raw_min_length = property_schema["minLength"]
        if (
            not isinstance(raw_min_length, int)
            or isinstance(raw_min_length, bool)
            or raw_min_length < 0
        ):
            raise _schema_invalid()
        min_length = raw_min_length
    if "maxLength" in property_schema:
        raw_max_length = property_schema["maxLength"]
        if (
            not isinstance(raw_max_length, int)
            or isinstance(raw_max_length, bool)
            or raw_max_length < 0
        ):
            raise _schema_invalid()
        max_length = raw_max_length
    if (
        (min_length is not None or max_length is not None)
        and "string" not in leaf_types
    ):
        raise _schema_invalid()
    if (
        min_length is not None
        and max_length is not None
        and min_length > max_length
    ):
        raise _schema_invalid()

    if value is None:
        return "null" in leaf_types
    if not isinstance(value, str) or "string" not in leaf_types:
        return False
    if min_length is not None and len(value) < min_length:
        return False
    if max_length is not None and len(value) > max_length:
        return False
    return True


def _validate_item_raw_schema(
    raw_value: dict[str, RawScalar | None],
    raw_schema: object,
) -> None:
    if not isinstance(raw_schema, Mapping):
        raise _schema_invalid()
    if not all(isinstance(key, str) for key in raw_schema) or not set(
        raw_schema
    ).issubset(_ITEM_SCHEMA_ROOT_KEYS):
        raise _schema_invalid()
    if raw_schema.get("type") != "object":
        raise _schema_invalid()

    properties = raw_schema.get("properties")
    if not isinstance(properties, Mapping) or not all(
        isinstance(key, str) for key in properties
    ):
        raise _schema_invalid()
    property_names = set(properties)
    for property_schema in properties.values():
        _raw_leaf_matches_schema(None, property_schema)

    if "required" in raw_schema:
        required_value = raw_schema["required"]
        if not isinstance(required_value, (list, tuple)) or not all(
            isinstance(item, str) for item in required_value
        ):
            raise _schema_invalid()
        required = tuple(required_value)
        if len(set(required)) != len(required) or not set(required).issubset(
            property_names
        ):
            raise _schema_invalid()
    else:
        required = ()
    if not set(required).issubset(raw_value):
        raise _schema_invalid()

    additional_properties = raw_schema.get("additionalProperties", True)
    if (
        additional_properties is not True
        and additional_properties is not False
        and not isinstance(additional_properties, Mapping)
    ):
        raise _schema_invalid()
    if isinstance(additional_properties, Mapping):
        _raw_leaf_matches_schema(None, additional_properties)

    unknown_keys = set(raw_value) - property_names
    if additional_properties is False and unknown_keys:
        raise _schema_invalid()

    for name, value in raw_value.items():
        property_schema = properties.get(name)
        if property_schema is None:
            if not isinstance(additional_properties, Mapping):
                continue
            property_schema = additional_properties
        if not _raw_leaf_matches_schema(value, property_schema):
            raise _schema_invalid()


def validate_envelope_for_module(
    envelope: SlotExtractionEnvelope,
    schema: ModuleSlotSchema,
) -> SlotExtractionEnvelope:
    if envelope.schema_version != schema.version:
        raise SlotExtractionError("slot_extraction_schema_invalid")

    definitions = {item.name: item for item in schema.definitions}
    counts = Counter(candidate.slot_name for candidate in envelope.candidates)
    for candidate in envelope.candidates:
        definition = definitions.get(candidate.slot_name)
        if definition is None:
            raise SlotExtractionError("slot_extraction_schema_invalid")
        if definition.raw_kind == "scalar" and not isinstance(candidate.raw_value, str):
            raise SlotExtractionError("slot_extraction_schema_invalid")
        if definition.raw_kind == "item" and not isinstance(candidate.raw_value, dict):
            raise SlotExtractionError("slot_extraction_schema_invalid")
        if (
            definition.raw_kind == "item"
            and isinstance(candidate.raw_value, dict)
            and definition.raw_schema is not None
        ):
            _validate_item_raw_schema(candidate.raw_value, definition.raw_schema)
        if counts[candidate.slot_name] > definition.max_candidates:
            raise SlotExtractionError("slot_extraction_schema_invalid")
        if not definition.repeatable and counts[candidate.slot_name] > definition.max_candidates:
            raise SlotExtractionError("slot_extraction_schema_invalid")
    return envelope
