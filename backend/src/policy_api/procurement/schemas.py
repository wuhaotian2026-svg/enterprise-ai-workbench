from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from policy_api.procurement.enums import (
    ProcurementCategoryCode,
    ProcurementCurrencyCode,
)


class ClosedProcurementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcurementRequestItemInput(ClosedProcurementInput):
    category_code: ProcurementCategoryCode
    item_name: str = Field(min_length=1, max_length=200)
    specification: str | None = Field(default=None, max_length=500)
    quantity: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
        allow_inf_nan=False,
    )
    unit: str = Field(min_length=1, max_length=40)
    estimated_unit_price: Decimal = Field(
        ge=0,
        max_digits=14,
        decimal_places=2,
        allow_inf_nan=False,
    )

    @field_validator("item_name", "specification", "unit", mode="before")
    @classmethod
    def trim_item_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("quantity", "estimated_unit_price", mode="before")
    @classmethod
    def reject_float_decimals(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, float):
            raise ValueError(f"{info.field_name}_must_not_be_float")
        return value


class ProcurementRequestInput(ClosedProcurementInput):
    title: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=2000)
    needed_by_date: date
    currency: ProcurementCurrencyCode
    items: list[ProcurementRequestItemInput] = Field(min_length=1, max_length=50)

    @field_validator("title", "purpose", mode="before")
    @classmethod
    def trim_request_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("needed_by_date", mode="before")
    @classmethod
    def require_iso_calendar_date(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            raise ValueError("needed_by_date_must_be_iso_date")
        if isinstance(value, date):
            return value
        if isinstance(value, str) and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value
        ):
            return value
        raise ValueError("needed_by_date_must_be_iso_date")

    @field_validator("needed_by_date")
    @classmethod
    def reject_past_needed_by_date(
        cls, value: date, info: ValidationInfo
    ) -> date:
        today = date.today()
        context = info.context
        if isinstance(context, Mapping) and "today" in context:
            injected_today = context["today"]
            if not isinstance(injected_today, date) or isinstance(
                injected_today, datetime
            ):
                raise ValueError("today_context_must_be_date")
            today = injected_today
        if value < today:
            raise ValueError("needed_by_date_before_today")
        return value
