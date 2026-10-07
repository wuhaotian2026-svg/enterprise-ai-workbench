from __future__ import annotations

from policy_api.slot_extraction.schemas import ModuleSlotDefinition, ModuleSlotSchema


HR_SLOT_SCHEMA = ModuleSlotSchema.build(
    module_key="hr",
    version="slot-extraction-v1",
    definitions=(
        ModuleSlotDefinition(
            name="leave_type_code",
            raw_kind="scalar",
            description=(
                "Copy the explicitly stated leave-type token verbatim, including 年假, "
                "调休, 补休, annual, annual_leave, compensatory, or comp_time. Extract "
                "it even in a correction or follow-up."
            ),
        ),
        ModuleSlotDefinition(
            name="start_date",
            raw_kind="scalar",
            description=(
                "Copy an explicitly and separately labelled start-date token verbatim; "
                "emit start_date when introduced by 开始日期 or 开始, even when a labelled "
                "end date is nearby. It is not part of one contiguous date range."
            ),
        ),
        ModuleSlotDefinition(
            name="end_date",
            raw_kind="scalar",
            description=(
                "Copy an explicitly and separately labelled end-date token verbatim; "
                "emit end_date when introduced by 结束日期 or 结束, even when a labelled "
                "start date is nearby. It is not part of one contiguous date range."
            ),
        ),
        ModuleSlotDefinition(
            name="date_range",
            raw_kind="scalar",
            description=(
                "Copy one contiguous phrase that explicitly contains both leave dates. "
                "The dates must be directly joined by 到, 至, -, —, or ~ without separate "
                "开始日期 and 结束日期 labels. With separate labels, must not emit date_range; "
                "otherwise emit only date_range and do not also emit start_date or end_date."
            ),
        ),
        ModuleSlotDefinition(
            name="reason",
            raw_kind="scalar",
            description=(
                "Copy only the explicitly stated leave-reason value. The source_quote may "
                "include 原因, 理由, 用于, or 补充原因, but raw_value must exclude the "
                "introducer and punctuation. Apply this boundary in a follow-up too."
            ),
        ),
        ModuleSlotDefinition(
            name="request_id",
            raw_kind="scalar",
            description=(
                "Copy an explicitly stated leave-request UUID verbatim, including when "
                "the user corrects or replaces the request identifier."
            ),
        ),
        ModuleSlotDefinition(
            name="year",
            raw_kind="scalar",
            description="Copy an explicitly stated four-digit year token verbatim.",
        ),
    ),
)


def hr_slot_schema() -> ModuleSlotSchema:
    return HR_SLOT_SCHEMA


__all__ = ["HR_SLOT_SCHEMA", "hr_slot_schema"]
