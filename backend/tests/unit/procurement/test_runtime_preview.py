from __future__ import annotations

import inspect
from datetime import date

import pytest
from pydantic import ValidationError

from policy_api.procurement.router import ProcurementPreviewResponse
from policy_api.procurement.runtime import ProcurementRuntime
from policy_api.procurement.schemas import ProcurementRequestInput
from policy_api.tools.errors import ToolError


class _ForbiddenDependency:
    def __getattribute__(self, name: str):
        raise AssertionError(f"preview accessed forbidden dependency: {name}")


def _request_input() -> ProcurementRequestInput:
    return ProcurementRequestInput.model_validate(
        {
            "title": "开发耗材",
            "purpose": "研发环境维护",
            "needed_by_date": "2035-01-01",
            "currency": "CNY",
            "items": [
                {
                    "category_code": "office_supplies",
                    "item_name": "签字笔",
                    "specification": None,
                    "quantity": "2",
                    "unit": "盒",
                    "estimated_unit_price": "10.00",
                },
                {
                    "category_code": "other",
                    "item_name": "测试耗材",
                    "specification": "小规格",
                    "quantity": "0.33",
                    "unit": "件",
                    "estimated_unit_price": "0.05",
                },
            ],
        },
        context={"today": date(2030, 1, 1)},
    )


def test_preview_request_has_no_business_context_and_uses_authoritative_total_calculation() -> None:
    dependency = _ForbiddenDependency()
    runtime = ProcurementRuntime(
        service=dependency,
        capability_resolver=dependency,
        request_reader=dependency,
        conversation_store=dependency,
        planner=dependency,
        approval_runtime=dependency,
        search_policy=dependency,
        observability=dependency,
    )

    result = runtime.preview_request(_request_input())

    assert list(inspect.signature(ProcurementRuntime.preview_request).parameters) == [
        "self",
        "request_input",
    ]
    assert result == {
        "currency": "CNY",
        "subtotals": ["20.00", "0.02"],
        "total": "20.02",
    }


def test_preview_request_maps_an_aggregate_total_overflow_to_a_stable_error() -> None:
    dependency = _ForbiddenDependency()
    runtime = ProcurementRuntime(
        service=dependency,
        capability_resolver=dependency,
        request_reader=dependency,
        conversation_store=dependency,
        planner=dependency,
        approval_runtime=dependency,
        search_policy=dependency,
        observability=dependency,
    )
    payload = _request_input().model_dump(mode="json")
    payload["items"] = [
        {
            **payload["items"][0],
            "quantity": "1",
            "estimated_unit_price": "999999999999.99",
        },
        {
            **payload["items"][0],
            "quantity": "1",
            "estimated_unit_price": "0.01",
        },
    ]
    request_input = ProcurementRequestInput.model_validate(
        payload, context={"today": date(2030, 1, 1)}
    )

    with pytest.raises(ToolError, match="total_amount_exceeds_maximum") as exc:
        runtime.preview_request(request_input)

    assert exc.value.code == "total_amount_exceeds_maximum"


@pytest.mark.parametrize(
    "payload",
    [
        {"currency": "CNY", "subtotals": ["20"], "total": "20.00"},
        {"currency": "CNY", "subtotals": ["20.00"], "total": "20.0"},
        {"currency": "CNY", "subtotals": [20], "total": "20.00"},
        {
            "currency": "CNY",
            "subtotals": ["100000000000000.00"],
            "total": "20.00",
        },
        {
            "currency": "CNY",
            "subtotals": ["20.00"],
            "total": "1000000000000.00",
        },
    ],
)
def test_preview_response_contract_rejects_noncanonical_or_unbounded_amounts(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ProcurementPreviewResponse.model_validate(payload)
