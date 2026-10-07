from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from decimal import Decimal, Inexact, localcontext

import pytest

from policy_api.procurement.calculation import ProcurementTotal, calculate_total
from policy_api.procurement.schemas import ProcurementRequestItemInput


@dataclass(frozen=True, slots=True)
class Line:
    quantity: object
    estimated_unit_price: object


def line(quantity: str, estimated_unit_price: str) -> Line:
    return Line(Decimal(quantity), Decimal(estimated_unit_price))


def test_total_uses_decimal_and_server_owned_subtotals() -> None:
    result = calculate_total(
        [
            line("3", "0.335"),
            line("2.50", "10.00"),
        ]
    )

    assert isinstance(result, ProcurementTotal)
    assert result.subtotals == (Decimal("1.01"), Decimal("25.00"))
    assert result.total_amount == Decimal("26.01")
    assert result.total_amount.as_tuple().exponent == -2


def test_calculation_does_not_inherit_ambient_inexact_trap() -> None:
    with localcontext() as context:
        context.traps[Inexact] = True
        result = calculate_total([line("3", "0.335")])

    assert result.subtotals == (Decimal("1.01"),)
    assert result.total_amount == Decimal("1.01")


def test_calculation_does_not_inherit_ambient_exponent_limits() -> None:
    with localcontext() as context:
        context.Emax = 9
        result = calculate_total([line("1", "999999999999.99")])

    assert result.subtotals == (Decimal("999999999999.99"),)
    assert result.total_amount == Decimal("999999999999.99")


def test_subtotals_keep_input_order_and_are_individually_rounded_half_up() -> None:
    result = calculate_total(
        [
            line("1", "1.004"),
            line("1", "1.005"),
            line("1", "1.006"),
        ]
    )

    assert result.subtotals == (
        Decimal("1.00"),
        Decimal("1.01"),
        Decimal("1.01"),
    )
    assert result.total_amount == Decimal("3.02")


def test_total_sums_rounded_subtotals_not_unrounded_products() -> None:
    result = calculate_total([line("1", "0.005"), line("1", "0.005")])

    assert result.subtotals == (Decimal("0.01"), Decimal("0.01"))
    assert result.total_amount == Decimal("0.02")


def test_zero_price_line_is_allowed_and_canonicalized() -> None:
    result = calculate_total([line("2.50", "0")])

    assert result.subtotals == (Decimal("0.00"),)
    assert result.total_amount == Decimal("0.00")


def test_negative_zero_price_is_canonicalized_to_unsigned_zero() -> None:
    result = calculate_total([line("2.50", "-0.00")])

    assert result.subtotals == (Decimal("0.00"),)
    assert result.subtotals[0].is_signed() is False
    assert result.total_amount == Decimal("0.00")
    assert result.total_amount.is_signed() is False


def test_schema_items_work_with_half_up_calculation_and_generator_input() -> None:
    item = ProcurementRequestItemInput.model_validate(
        {
            "category_code": "office_supplies",
            "item_name": "Paper",
            "quantity": Decimal("1.01"),
            "unit": "ream",
            "estimated_unit_price": Decimal("0.50"),
        }
    )

    result = calculate_total(entry for entry in [item])

    assert result.subtotals == (Decimal("0.51"),)
    assert result.total_amount == Decimal("0.51")


def test_result_is_immutable() -> None:
    result = calculate_total([line("1", "1")])

    with pytest.raises(FrozenInstanceError):
        result.total_amount = Decimal("2.00")


@pytest.mark.parametrize("field", ["quantity", "estimated_unit_price"])
@pytest.mark.parametrize("value", ["1.00", 1, 1.0, True, None])
def test_calculation_rejects_non_decimal_inputs(field: str, value: object) -> None:
    values: dict[str, object] = {
        "quantity": Decimal("1.00"),
        "estimated_unit_price": Decimal("1.00"),
    }
    values[field] = value

    with pytest.raises(TypeError, match=f"{field}_must_be_decimal"):
        calculate_total([Line(**values)])


@pytest.mark.parametrize("field", ["quantity", "estimated_unit_price"])
@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_calculation_rejects_non_finite_decimals(field: str, value: Decimal) -> None:
    values: dict[str, object] = {
        "quantity": Decimal("1.00"),
        "estimated_unit_price": Decimal("1.00"),
    }
    values[field] = value

    with pytest.raises(ValueError, match=f"{field}_must_be_finite"):
        calculate_total([Line(**values)])


@pytest.mark.parametrize("quantity", [Decimal("0"), Decimal("-0.01")])
def test_calculation_rejects_non_positive_quantity(quantity: Decimal) -> None:
    with pytest.raises(ValueError, match="quantity_must_be_positive"):
        calculate_total([Line(quantity, Decimal("1.00"))])


def test_calculation_rejects_negative_price() -> None:
    with pytest.raises(ValueError, match="estimated_unit_price_must_be_nonnegative"):
        calculate_total([Line(Decimal("1.00"), Decimal("-0.01"))])


def test_total_maximum_is_inclusive() -> None:
    result = calculate_total([line("1", "999999999999.99")])

    assert result.total_amount == Decimal("999999999999.99")


@pytest.mark.parametrize(
    "lines",
    [
        [line("1", "1000000000000.00")],
        [line("1", "999999999999.99"), line("1", "0.01")],
        [line("1E+1000", "1")],
    ],
)
def test_total_over_maximum_is_rejected_without_clamping(lines: list[Line]) -> None:
    with pytest.raises(ValueError, match="total_amount_exceeds_maximum"):
        calculate_total(lines)
