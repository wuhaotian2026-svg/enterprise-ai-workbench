from __future__ import annotations

from policy_api.models import StringEnum


class ProcurementCategoryCode(StringEnum):
    OFFICE_SUPPLIES = "office_supplies"
    IT_EQUIPMENT = "it_equipment"
    SOFTWARE_SERVICE = "software_service"
    PROFESSIONAL_SERVICE = "professional_service"
    OTHER = "other"


class ProcurementCurrencyCode(StringEnum):
    CNY = "CNY"


class ProcurementCommandOperationStatus(StringEnum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
