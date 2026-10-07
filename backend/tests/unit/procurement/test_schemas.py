from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from policy_api.procurement.enums import ProcurementCategoryCode, ProcurementCurrencyCode
from policy_api.procurement.schemas import (
    ProcurementRequestInput,
    ProcurementRequestItemInput,
)


TODAY = date(2026, 8, 24)


def valid_payload() -> dict[str, object]:
    return {
        "title": "Office refresh",
        "purpose": "Replace worn equipment",
        "needed_by_date": "2026-08-24",
        "currency": "CNY",
        "items": [
            {
                "category_code": "office_supplies",
                "item_name": "Ergonomic keyboard",
                "specification": None,
                "quantity": "2.00",
                "unit": "piece",
                "estimated_unit_price": "399.50",
            }
        ],
    }


def validate(payload: dict[str, object]) -> ProcurementRequestInput:
    return ProcurementRequestInput.model_validate(payload, context={"today": TODAY})


def test_request_is_closed_and_preserves_valid_typed_values() -> None:
    payload = valid_payload()
    payload.update(
        {
            "title": f"  {'T' * 160}  ",
            "purpose": "  Replace  worn equipment  ",
        }
    )
    item = payload["items"][0]
    assert isinstance(item, dict)
    item.update(
        {
            "item_name": f"  {'I' * 200}  ",
            "specification": "  USB-C  layout  ",
            "unit": f"  {'u' * 40}  ",
        }
    )

    request = validate(payload)

    assert request.title == "T" * 160
    assert request.purpose == "Replace  worn equipment"
    assert request.needed_by_date == TODAY
    assert request.currency is ProcurementCurrencyCode.CNY
    assert request.items[0].category_code is ProcurementCategoryCode.OFFICE_SUPPLIES
    assert request.items[0].item_name == "I" * 200
    assert request.items[0].specification == "USB-C  layout"
    assert request.items[0].quantity == Decimal("2.00")
    assert request.items[0].unit == "u" * 40
    assert request.items[0].estimated_unit_price == Decimal("399.50")
    assert ProcurementRequestInput.model_config["extra"] == "forbid"
    assert ProcurementRequestItemInput.model_config["extra"] == "forbid"


def test_specification_allows_none_and_preserves_trimmed_empty_string() -> None:
    payload = valid_payload()
    item = payload["items"][0]
    assert isinstance(item, dict)
    item["specification"] = "   "

    request = validate(payload)

    assert request.items[0].specification == ""

    omitted_payload = valid_payload()
    omitted_item = omitted_payload["items"][0]
    assert isinstance(omitted_item, dict)
    del omitted_item["specification"]
    assert validate(omitted_payload).items[0].specification is None


@pytest.mark.parametrize(
    "field",
    ["actor", "status", "subtotal", "total", "total_amount", "manager"],
)
def test_request_rejects_security_and_derived_extra_fields(field: str) -> None:
    payload = valid_payload()
    payload[field] = "untrusted"

    with pytest.raises(ValidationError):
        validate(payload)


@pytest.mark.parametrize(
    "field",
    ["actor", "status", "subtotal", "total", "total_amount", "manager"],
)
def test_item_rejects_security_and_derived_extra_fields(field: str) -> None:
    payload = valid_payload()
    item = payload["items"][0]
    assert isinstance(item, dict)
    item[field] = "untrusted"

    with pytest.raises(ValidationError):
        validate(payload)


@pytest.mark.parametrize(
    "field",
    ["title", "purpose", "needed_by_date", "currency", "items"],
)
def test_request_does_not_invent_missing_business_fields(field: str) -> None:
    payload = valid_payload()
    del payload[field]

    with pytest.raises(ValidationError):
        validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "category_code",
        "item_name",
        "quantity",
        "unit",
        "estimated_unit_price",
    ],
)
def test_item_does_not_invent_missing_business_fields(field: str) -> None:
    payload = valid_payload()
    item = payload["items"][0]
    assert isinstance(item, dict)
    del item[field]

    with pytest.raises(ValidationError):
        validate(payload)


def test_needed_by_date_uses_injected_today_boundary() -> None:
    same_day = validate(valid_payload())
    assert same_day.needed_by_date == TODAY

    prior_payload = valid_payload()
    prior_payload["needed_by_date"] = "2026-08-23"
    with pytest.raises(ValidationError, match="needed_by_date_before_today"):
        validate(prior_payload)

    natural_language_payload = valid_payload()
    natural_language_payload["needed_by_date"] = "next Friday"
    with pytest.raises(ValidationError):
        validate(natural_language_payload)


@pytest.mark.parametrize(
    "value",
    [
        datetime(2099, 1, 1),
        "2099-01-01T00:00:00Z",
        4_070_908_800,
    ],
)
def test_needed_by_date_rejects_non_calendar_date_wire_shapes(value: object) -> None:
    payload = valid_payload()
    payload["needed_by_date"] = value

    with pytest.raises(ValidationError, match="needed_by_date_must_be_iso_date"):
        validate(payload)


@pytest.mark.parametrize("value", [date(2099, 1, 1), "2099-01-01"])
def test_needed_by_date_accepts_date_and_exact_iso_date_string(value: object) -> None:
    payload = valid_payload()
    payload["needed_by_date"] = value

    assert validate(payload).needed_by_date == date(2099, 1, 1)


@pytest.mark.parametrize("injected_today", ["2026-08-24", datetime(2026, 8, 24)])
def test_needed_by_date_rejects_invalid_today_context(
    injected_today: object,
) -> None:
    with pytest.raises(ValidationError, match="today_context_must_be_date"):
        ProcurementRequestInput.model_validate(
            valid_payload(), context={"today": injected_today}
        )


@pytest.mark.parametrize("value", ["USD", "cny", "CNY ", ""])
def test_currency_is_the_closed_cny_enum(value: str) -> None:
    payload = valid_payload()
    payload["currency"] = value

    with pytest.raises(ValidationError):
        validate(payload)


def test_items_require_between_one_and_fifty_lines_and_keep_order() -> None:
    empty_payload = valid_payload()
    empty_payload["items"] = []
    with pytest.raises(ValidationError):
        validate(empty_payload)

    oversized_payload = valid_payload()
    item = oversized_payload["items"][0]
    oversized_payload["items"] = [deepcopy(item) for _ in range(51)]
    with pytest.raises(ValidationError):
        validate(oversized_payload)

    ordered_payload = valid_payload()
    first = ordered_payload["items"][0]
    second = deepcopy(first)
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    first["item_name"] = "first"
    second["item_name"] = "second"
    ordered_payload["items"] = [first, second]
    assert [item.item_name for item in validate(ordered_payload).items] == [
        "first",
        "second",
    ]


def test_schema_accepts_all_minimum_inclusive_boundaries() -> None:
    payload = valid_payload()
    payload["title"] = "t"
    payload["purpose"] = "p"
    item = payload["items"][0]
    assert isinstance(item, dict)
    item["item_name"] = "i"
    item["unit"] = "u"
    item["quantity"] = Decimal("0.01")

    request = validate(payload)

    assert request.title == "t"
    assert request.purpose == "p"
    assert request.items[0].item_name == "i"
    assert request.items[0].unit == "u"
    assert request.items[0].quantity == Decimal("0.01")


def test_schema_accepts_maximum_text_and_item_count_boundaries_in_order() -> None:
    payload = valid_payload()
    payload["purpose"] = "p" * 2000
    item_template = payload["items"][0]
    assert isinstance(item_template, dict)
    expected_names = [f"item-{index:02d}" for index in range(50)]
    items: list[dict[str, object]] = []
    for item_name in expected_names:
        item = deepcopy(item_template)
        item["item_name"] = item_name
        item["specification"] = "s" * 500
        items.append(item)
    payload["items"] = items

    request = validate(payload)

    assert request.purpose == "p" * 2000
    assert len(request.items) == 50
    assert [item.item_name for item in request.items] == expected_names
    assert all(item.specification == "s" * 500 for item in request.items)


@pytest.mark.parametrize(
    "category",
    [
        "office_supplies",
        "it_equipment",
        "software_service",
        "professional_service",
        "other",
    ],
)
def test_category_uses_the_existing_closed_catalog(category: str) -> None:
    payload = valid_payload()
    item = payload["items"][0]
    assert isinstance(item, dict)
    item["category_code"] = category

    assert validate(payload).items[0].category_code.value == category


def test_category_rejects_unknown_values_without_inference() -> None:
    payload = valid_payload()
    item = payload["items"][0]
    assert isinstance(item, dict)
    item["category_code"] = "computers"

    with pytest.raises(ValidationError):
        validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "   "),
        ("title", "x" * 161),
        ("purpose", "   "),
        ("purpose", "x" * 2001),
    ],
)
def test_request_text_limits_apply_after_trimming(field: str, value: str) -> None:
    payload = valid_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("item_name", "   "),
        ("item_name", "x" * 201),
        ("specification", "x" * 501),
        ("unit", "   "),
        ("unit", "x" * 41),
    ],
)
def test_item_text_limits_apply_after_trimming(field: str, value: str) -> None:
    payload = valid_payload()
    item = payload["items"][0]
    assert isinstance(item, dict)
    item[field] = value

    with pytest.raises(ValidationError):
        validate(payload)


@pytest.mark.parametrize("field", ["quantity", "estimated_unit_price"])
def test_decimal_inputs_reject_binary_float_values(field: str) -> None:
    payload = valid_payload()
    item = payload["items"][0]
    assert isinstance(item, dict)
    item[field] = 1.25

    with pytest.raises(ValidationError, match=f"{field}_must_not_be_float"):
        validate(payload)


@pytest.mark.parametrize("value", ["0", "-0.01"])
def test_quantity_must_be_positive(value: str) -> None:
    payload = valid_payload()
    item = payload["items"][0]
    assert isinstance(item, dict)
    item["quantity"] = value

    with pytest.raises(ValidationError):
        validate(payload)


def test_zero_estimated_unit_price_is_allowed() -> None:
    payload = valid_payload()
    item = payload["items"][0]
    assert isinstance(item, dict)
    item["estimated_unit_price"] = "0"

    assert validate(payload).items[0].estimated_unit_price == Decimal("0")


def test_decimal_precision_limits_are_inclusive() -> None:
    payload = valid_payload()
    item = payload["items"][0]
    assert isinstance(item, dict)
    item["quantity"] = "9999999999.99"
    item["estimated_unit_price"] = "999999999999.99"

    validated_item = validate(payload).items[0]

    assert validated_item.quantity == Decimal("9999999999.99")
    assert validated_item.estimated_unit_price == Decimal("999999999999.99")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", "1.001"),
        ("quantity", "10000000000.00"),
        ("estimated_unit_price", "0.001"),
        ("estimated_unit_price", "1000000000000.00"),
        ("estimated_unit_price", "-0.01"),
        ("quantity", "NaN"),
        ("estimated_unit_price", "Infinity"),
    ],
)
def test_decimal_shape_and_finite_boundaries_are_enforced(
    field: str, value: str
) -> None:
    payload = valid_payload()
    item = payload["items"][0]
    assert isinstance(item, dict)
    item[field] = value

    with pytest.raises(ValidationError):
        validate(payload)
