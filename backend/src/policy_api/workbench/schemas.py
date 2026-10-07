from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from policy_api.workbench.capabilities import Capability, ScopeKind


class ModuleResponse(BaseModel):
    key: str
    label: str
    index: str


class ModuleListResponse(BaseModel):
    modules: list[ModuleResponse]
    catalog_version: Literal["2026-08-23"] = "2026-08-23"


class MetricValue(BaseModel):
    numerator: int | float | None
    denominator: int | float | None
    value: float | None
    available: bool
    sample_size: int = Field(ge=0)
    metric_version: Literal["v1"] = "v1"


class AnalyticsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    start: datetime = Field(alias="from")
    end: datetime = Field(alias="to")
    organization_unit_id: UUID | None
    metric_version: Literal["v1"] = "v1"
    metrics: dict[str, MetricValue]


class ClosedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrganizationUnitCreate(ClosedRequest):
    client_operation_id: UUID
    code: str = Field(min_length=1, max_length=60, pattern=r"^[A-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    parent_id: UUID | None = None


class OrganizationUnitUpdate(ClosedRequest):
    client_operation_id: UUID
    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=60,
        pattern=r"^[A-Z0-9_-]+$",
    )
    name: str | None = Field(default=None, min_length=1, max_length=120)
    parent_id: UUID | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def require_business_change(self) -> "OrganizationUnitUpdate":
        changes = self.model_fields_set - {"client_operation_id"}
        if not changes:
            raise ValueError("organization_unit_update_empty")
        if "code" in changes and self.code is None:
            raise ValueError("organization_unit_code_invalid")
        if "name" in changes and self.name is None:
            raise ValueError("organization_unit_name_invalid")
        if "is_active" in changes and self.is_active is None:
            raise ValueError("organization_unit_status_invalid")
        return self


class EmployeeAssignmentUpdate(ClosedRequest):
    client_operation_id: UUID
    organization_unit_id: UUID | None
    manager_employee_id: UUID | None


class CapabilityGrantCreate(ClosedRequest):
    client_operation_id: UUID
    user_id: UUID
    capability: Capability
    scope_kind: ScopeKind
    organization_unit_id: UUID | None = None


class OperationRequest(ClosedRequest):
    client_operation_id: UUID


class UiEventRequest(ClosedRequest):
    event_id: UUID
    event_name: str = Field(min_length=1, max_length=80)
    dimensions: dict[str, object]


class OrganizationUnitResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    parent_id: UUID | None
    is_active: bool


class EmployeeAssignmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    employee_number: str
    display_name: str
    organization_unit_id: UUID | None
    manager_employee_id: UUID | None
    is_active: bool


class CapabilityGrantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    capability: str
    scope_kind: str
    organization_unit_id: UUID | None
    is_active: bool
