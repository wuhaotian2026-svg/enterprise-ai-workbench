from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from policy_api.hr.models import EmployeeProfile
from policy_api.hr.runtime import HrRuntime
from policy_api.models import User
from policy_api.workbench.analytics import AnalyticsService
from policy_api.workbench.capabilities import CapabilityResolver, OrganizationService
from policy_api.workbench.catalog import ModuleCatalog
from policy_api.workbench.events import (
    EventInput,
    ProductEvent,
    ProductEventEmitter,
    ProductEventValidationError,
    validate_event_dimensions,
)


@dataclass(slots=True)
class WorkbenchRuntime:
    module_catalog: ModuleCatalog
    capability_resolver: CapabilityResolver
    organization_service: OrganizationService = field(
        default_factory=OrganizationService
    )
    product_event_emitter: ProductEventEmitter = field(
        default_factory=ProductEventEmitter
    )
    analytics_service: AnalyticsService = field(default_factory=AnalyticsService)
    hr_runtime: HrRuntime | None = None

    def record_ui_event(
        self,
        db: Session,
        *,
        actor: User,
        event_id: UUID,
        event_name: str,
        dimensions: Mapping[str, object],
        request_id: str | None,
    ) -> ProductEvent:
        if event_name != "workbench_module_opened":
            raise ProductEventValidationError("event_name_not_allowed")
        normalized_dimensions = validate_event_dimensions(event_name, dimensions)
        module_key = normalized_dimensions.get("module_key")
        allowed_keys = {
            item.key for item in self.module_catalog.allowed_modules(db, actor)
        }
        if not isinstance(module_key, str) or module_key not in allowed_keys:
            raise ProductEventValidationError("event_dimensions_invalid")
        organization_unit_id = db.scalar(
            select(EmployeeProfile.organization_unit_id).where(
                EmployeeProfile.user_id == actor.id,
                EmployeeProfile.is_active.is_(True),
            )
        )
        return self.product_event_emitter.append(
            db,
            EventInput(
                event_id=event_id,
                event_name=event_name,
                module_key=module_key,
                actor_user_id=actor.id,
                organization_unit_id=organization_unit_id,
                role_snapshot=actor.role.value,
                request_id=request_id,
                outcome="opened",
                duration_ms=None,
                dimensions=normalized_dimensions,
            ),
        )
