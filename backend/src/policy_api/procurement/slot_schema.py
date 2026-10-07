from __future__ import annotations

from policy_api.slot_extraction.schemas import ModuleSlotDefinition, ModuleSlotSchema


def _raw_string(
    description: str,
    *,
    examples: list[str] | None = None,
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 2000,
        "description": description,
    }
    if examples is not None:
        schema["examples"] = examples
    return schema


def _nullable_raw_string(
    description: str,
    *,
    examples: list[str] | None = None,
) -> dict[str, object]:
    schema: dict[str, object] = {
        "anyOf": [
            _raw_string(description, examples=examples),
            {"type": "null"},
        ],
        "description": description,
    }
    if examples is not None:
        schema["examples"] = examples
    return schema


_ITEM_RAW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["unit", "category_hint"],
    "properties": {
        "item_name": _nullable_raw_string(
            "Copy only the item-name token verbatim."
        ),
        "specification": _nullable_raw_string(
            "Copy only an explicitly stated specification token verbatim."
        ),
        "quantity": _nullable_raw_string(
            "Copy only an explicitly stated numeric or Chinese-numeral quantity "
            "expression verbatim without the unit when it is stated separately. Only "
            "the closed generic classifier 个 may remain attached to the count. For "
            "attached wording 一个桌子, emit quantity \"一个\" and unit null; that "
            "attached classifier is not unit evidence. Separate wording 单位个 or 按个 "
            "is explicit unit evidence; copy any independently explicit quantity. Thus "
            "数量一，单位个 or 数量一，按个 emits quantity \"一\" and unit \"个\". Explicit "
            "business units 个, 张, 台, 把, 套, 项, 箱, 件, 本, 支, 份, 次, 人天, 月 "
            "must otherwise be emitted in the separate unit leaf and must not remain "
            "in quantity. The validator must never infer a unit from raw quantity.",
            examples=["2", "三", "一个"],
        ),
        "unit": _nullable_raw_string(
            "Always include this required unit leaf. Copy only an explicit business-unit "
            "token verbatim, including 个 when the user separately says 单位个 or 按个. "
            "When the count is also explicit, 数量一，单位个 or 数量一，按个 emits "
            "quantity \"一\" and unit \"个\". For attached "
            "classifier wording 一个桌子, emit quantity \"一个\" and unit null. When "
            "there is no explicit unit evidence in the current turn, set unit to null; "
            "never infer or derive it from raw quantity. Other examples include 张, 台, "
            "把, 套, 项, 箱, 件, 本, 支, 份, 次, 人天, and 月.",
            examples=[
                "个", "张", "台", "把", "套", "项", "箱", "件",
                "本", "支", "份", "次", "人天", "月",
            ],
        ),
        "estimated_unit_price": _nullable_raw_string(
            "Copy only the price number token without currency words, currency symbols, 元, or a unit.",
            examples=["500", "399.50"],
        ),
        "category_hint": _nullable_raw_string(
            "Copy an explicit category phrase only; never infer a category. "
            "When the item phrase explicitly contains a category marker such as "
            "办公用品类 or IT设备类, copy that exact marker. Set this leaf to null "
            "only when the user provided no category so the item remains partial.",
            examples=["办公用品类", "IT设备类", "软件服务类", "专业服务类"],
        ),
    },
}


PROCUREMENT_SLOT_SCHEMA = ModuleSlotSchema.build(
    module_key="procurement",
    version="slot-extraction-v1",
    definitions=(
        ModuleSlotDefinition(
            "title",
            "scalar",
            description=(
                "Copy only the procurement title value verbatim; exclude labels such as "
                "标题, 采购标题, or 申请标题. A label-only string is not a field value. "
                "References to assistant or history content are not explicit values in "
                "the current turn; do not emit a candidate for them. The source_quote "
                "may include a label to be unique."
            ),
        ),
        ModuleSlotDefinition(
            "purpose",
            "scalar",
            description=(
                "Recognize an explicit procurement purpose expressed with labels 用途 or "
                "采购用途, or natural relations 用于……, 用来……, 给……使用, and "
                "供……使用. Copy only the procurement purpose value verbatim into "
                "raw_value; exclude the purpose introducer, label, recipient marker, and "
                "trailing 使用 relation. A label-only or relation-only string is not a "
                "field value. The source_quote may include the complete relation so it is "
                "uniquely verifiable, but it must be an exact quote from the current user "
                "turn. References to assistant or history content are not explicit values "
                "in the current turn; do not emit a candidate for them."
            ),
        ),
        ModuleSlotDefinition(
            "needed_by_date",
            "scalar",
            description="Copy the explicitly stated needed-date token verbatim; do not convert its format.",
        ),
        ModuleSlotDefinition(
            "needed_by_year",
            "scalar",
            description=(
                "Copy only an explicitly stated four-digit year intended for the "
                "procurement needed date, including natural forms such as 2026年, "
                "年份2026, 年份是2026, or 就是2026年. This is a helper for completing "
                "an explicitly provided month/day. Never infer a year, emit relative "
                "years such as 明年, or copy a year from history, assistant text, or "
                "Tool output."
            ),
        ),
        ModuleSlotDefinition(
            "currency",
            "scalar",
            description="Copy the explicit currency token verbatim, such as 人民币 or CNY.",
        ),
        ModuleSlotDefinition(
            "items",
            "item",
            repeatable=True,
            max_candidates=50,
            description=(
                "Emit one candidate for each explicitly stated item phrase. Every "
                "non-null leaf must be copied verbatim from the same source_quote; "
                "omit an optional leaf when it is not explicit. Always include unit and "
                "category_hint; set either required leaf to null without explicit "
                "evidence. Keep quantity, unit, and "
                "price as separate JSON-string leaves: for example quantity \"三\" and "
                "unit \"把\", never quantity \"三把\"; price \"500\", never \"500元\". "
                "Only the closed generic classifier 个 may remain attached to a count, "
                "as in attached wording 一个桌子: emit quantity \"一个\" and unit null. "
                "Separate wording 单位个 or 按个 is explicit unit evidence; copy any "
                "independently explicit quantity. Thus 数量一，单位个 or 数量一，按个 "
                "emits quantity \"一\" and unit \"个\". Explicit business "
                "units 个, 张, 台, 把, 套, 项, 箱, 件, 本, 支, 份, 次, 人天, 月 must "
                "otherwise be emitted in the separate unit leaf and must not remain in "
                "quantity; never infer or derive a unit from raw quantity. Copy an "
                "explicit category marker verbatim, including a trailing 类, or use null "
                "when no category was stated. Never infer it; without an explicit "
                "category the item must remain partial."
            ),
            raw_schema=_ITEM_RAW_SCHEMA,
        ),
        ModuleSlotDefinition(
            "request_id",
            "scalar",
            description="Copy an explicitly stated procurement-request UUID verbatim, including a correction.",
        ),
        ModuleSlotDefinition(
            "task_id",
            "scalar",
            description="Copy an explicitly stated approval-task UUID verbatim.",
        ),
        ModuleSlotDefinition(
            "reason",
            "scalar",
            description=(
                "Copy the explicitly stated rejection or withdrawal reason value "
                "verbatim, including a follow-up. 原因, 拒绝原因, 驳回原因, and 撤回原因 "
                "are label-only strings, not values. References to assistant or history "
                "content are not explicit values in the current turn; do not emit a "
                "candidate for them."
            ),
        ),
        ModuleSlotDefinition(
            "comment",
            "scalar",
            description=(
                "Copy the explicitly stated approval comment value verbatim, including "
                "a follow-up. 意见, 审批意见, and 补充审批意见 are label-only strings, "
                "not values. References to assistant or history content are not explicit "
                "values in the current turn; do not emit a candidate for them."
            ),
        ),
    ),
)


def procurement_slot_schema() -> ModuleSlotSchema:
    return PROCUREMENT_SLOT_SCHEMA


__all__ = ["PROCUREMENT_SLOT_SCHEMA", "procurement_slot_schema"]
