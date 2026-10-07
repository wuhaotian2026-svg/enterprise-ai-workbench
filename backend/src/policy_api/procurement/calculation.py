from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    DecimalException,
    localcontext,
)
from typing import Protocol


CENT = Decimal("0.01")
MAX_TOTAL_AMOUNT = Decimal("999999999999.99")
_ZERO_AMOUNT = Decimal("0.00")


class ProcurementAmountLine(Protocol):
    quantity: Decimal
    estimated_unit_price: Decimal


@dataclass(frozen=True, slots=True)
class ProcurementTotal:
    subtotals: tuple[Decimal, ...]
    total_amount: Decimal


def _require_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name}_must_be_decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name}_must_be_finite")
    return value


def _owned_context(precision: int) -> Context:
    return Context(
        prec=precision,
        rounding=ROUND_HALF_UP,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[],
    )


def calculate_total(lines: Iterable[ProcurementAmountLine]) -> ProcurementTotal:
    subtotals: list[Decimal] = []
    total_amount = _ZERO_AMOUNT

    for line in lines:
        quantity = _require_decimal(line.quantity, "quantity")
        estimated_unit_price = _require_decimal(
            line.estimated_unit_price, "estimated_unit_price"
        )
        if quantity <= 0:
            raise ValueError("quantity_must_be_positive")
        if estimated_unit_price < 0:
            raise ValueError("estimated_unit_price_must_be_nonnegative")

        if (
            estimated_unit_price != 0
            and quantity.adjusted() + estimated_unit_price.adjusted()
            > MAX_TOTAL_AMOUNT.adjusted()
        ):
            raise ValueError("total_amount_exceeds_maximum")

        precision = max(
            32,
            len(quantity.as_tuple().digits)
            + len(estimated_unit_price.as_tuple().digits)
            + 4,
        )
        try:
            with localcontext(_owned_context(precision)):
                subtotal = (quantity * estimated_unit_price).quantize(
                    CENT, rounding=ROUND_HALF_UP
                )
                if subtotal.is_zero():
                    subtotal = _ZERO_AMOUNT
                next_total = (total_amount + subtotal).quantize(CENT)
                if next_total.is_zero():
                    next_total = _ZERO_AMOUNT
        except DecimalException as exc:
            raise ValueError("amount_calculation_invalid") from exc

        if (
            not subtotal.is_finite()
            or not next_total.is_finite()
            or subtotal > MAX_TOTAL_AMOUNT
            or next_total > MAX_TOTAL_AMOUNT
        ):
            raise ValueError("total_amount_exceeds_maximum")
        subtotals.append(subtotal)
        total_amount = next_total

    return ProcurementTotal(tuple(subtotals), total_amount)
