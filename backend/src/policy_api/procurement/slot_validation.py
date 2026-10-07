from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, ValidationError

from policy_api.procurement.calculation import MAX_TOTAL_AMOUNT
from policy_api.procurement.slot_schema import PROCUREMENT_SLOT_SCHEMA
from policy_api.slot_extraction.errors import (
    SourceQuoteAmbiguous,
    SourceQuoteNotFound,
)
from policy_api.slot_extraction.merge import (
    AcceptedCandidate,
    CandidateProvenance,
    CandidateValidationResult,
    PendingCandidate,
    RejectedCandidate,
)
from policy_api.slot_extraction.normalization import (
    SourceQuoteMatch,
    locate_source_quote,
    normalize_with_spans,
)
from policy_api.slot_extraction.schemas import (
    RawScalar,
    SlotCandidate,
    SlotExtractionEnvelope,
    validate_envelope_for_module,
)


PROCUREMENT_SLOT_VALIDATOR_VERSION = "procurement-slot-validator-v6"
_MIN_YEAR = 2000
_MAX_YEAR = 2100
_ITEM_KEYS = frozenset(
    {
        "item_name",
        "specification",
        "quantity",
        "unit",
        "estimated_unit_price",
        "category_hint",
    }
)
_ITEM_REQUIRED = (
    "item_name",
    "quantity",
    "unit",
    "estimated_unit_price",
    "category_code",
)
_TEXT_LIMITS = {
    "title": 160,
    "purpose": 2000,
    "reason": 2000,
    "comment": 2000,
}
_LABEL_ONLY_VALUES = {
    "title": frozenset({"标题", "采购标题", "申请标题"}),
    "purpose": frozenset({"用途", "采购用途"}),
    "reason": frozenset({"原因", "拒绝原因", "驳回原因", "撤回原因"}),
    "comment": frozenset({"意见", "审批意见", "补充审批意见"}),
}
_ITEM_TEXT_LIMITS = {
    "item_name": 200,
    "specification": 500,
    "unit": 40,
    "category_hint": 200,
}
_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_MONEY_SUFFIXES = ("块钱", "人民币", "元", "块")
_QUANTITY_MEASURE_SUFFIXES = (
    "人天",
    "个月",
    "个",
    "台",
    "把",
    "张",
    "项",
    "套",
    "箱",
    "件",
    "本",
    "支",
    "份",
    "次",
)
_APPROXIMATE_NUMBER_PREFIXES = (
    "大概是",
    "大约是",
    "差不多是",
    "大概",
    "大约",
    "差不多",
    "不到",
    "约为",
    "约",
)
_APPROXIMATE_NUMBER_SUFFIXES = ("左右", "上下", "多")
_APPROXIMATE_QUANTITY_PAIRS = frozenset(
    {"一两", "两三", "三四", "四五", "五六", "六七", "七八", "八九"}
)
_EXPLICIT_GENERIC_UNIT = re.compile(
    r"(?:(?:计量)?单位\s*(?:是|为|[:：])?\s*个)|(?:按\s*个)"
)
_STANDALONE_GENERIC_UNIT = re.compile(
    r"(?:^|[，,；;、：:\s])个(?=$|[，,；;、。.!！?？\s])"
)
_CATEGORY_ALIASES = {
    "office_supplies": "office_supplies",
    "办公用品": "office_supplies",
    "办公用品类": "office_supplies",
    "办公类": "office_supplies",
    "it_equipment": "it_equipment",
    "it设备": "it_equipment",
    "it设备类": "it_equipment",
    "信息技术设备": "it_equipment",
    "software_service": "software_service",
    "软件服务": "software_service",
    "软件服务类": "software_service",
    "软件订阅": "software_service",
    "professional_service": "professional_service",
    "专业服务": "professional_service",
    "专业服务类": "professional_service",
    "other": "other",
    "其他": "other",
}
_RELATIVE_DATES = {"今天": 0, "明天": 1, "后天": 2}
_AMBIGUOUS_DATE_WORDS = frozenset(
    {"下个月", "月底", "月底前", "尽快", "近期", "下周", "下星期"}
)
_FULL_DATE = re.compile(r"^(?P<y>\d{4})[-./](?P<m>\d{1,2})[-./](?P<d>\d{1,2})$")
_FULL_CN_DATE = re.compile(
    r"^(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})(?:日|号)?$"
)
_PARTIAL_DATE = re.compile(
    r"^(?P<m>\d{1,2})(?:[.]|月)(?P<d>\d{1,2})(?:日|号)?$"
)
_YEAR_VALUE = re.compile(
    r"^(?:(?:年份(?:是)?)|(?:就是))?(?P<year>\d{4})年?$"
)
_YEAR_TOKEN = re.compile(r"(?<!\d)\d{4}(?!\d)")
_YEAR_APPROXIMATE_PREFIX = re.compile(
    r"(?:大概|大约|约|差不多|预计|估计|大致|可能(?:是)?)\s*"
    r"(?:年份?\s*(?:是)?)?\s*$"
)
_YEAR_APPROXIMATE_SUFFIX = re.compile(r"^\s*年?\s*(?:左右|前后|上下)")
_DECIMAL_TEXT = re.compile(r"^\d+(?:\.\d{1,2})?$")
_NUMERIC_SOURCE_CHARS = frozenset("0123456789.零〇一二两三四五六七八九十百千万")
_NEGATIVE_PREFIXES = frozenset("-−﹣负")

ProcurementCandidateValidationResult = CandidateValidationResult


class ProcurementItemRaw(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item_name: RawScalar | None = None
    specification: RawScalar | None = None
    quantity: RawScalar | None = None
    unit: RawScalar | None
    estimated_unit_price: RawScalar | None = None
    category_hint: RawScalar | None


@dataclass(frozen=True, slots=True)
class _ValidatedItem:
    item_ref: str
    fields: dict[str, object]
    field_sources: dict[str, object]
    missing_fields: tuple[str, ...]
    rejected: tuple[RejectedCandidate, ...]
    provenance: CandidateProvenance | None
    unresolved_reason_code: str | None = None

    @property
    def complete(self) -> bool:
        return not self.missing_fields and not self.rejected

    def canonical_item(self) -> dict[str, object]:
        if not self.complete:
            raise ValueError("procurement_item_not_complete")
        return {
            "category_code": self.fields["category_code"],
            "item_name": self.fields["item_name"],
            "specification": self.fields.get("specification"),
            "quantity": self.fields["quantity"],
            "unit": self.fields["unit"],
            "estimated_unit_price": self.fields["estimated_unit_price"],
        }

    def pending_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "item_ref": self.item_ref,
            "fields": dict(self.fields),
            "field_sources": dict(self.field_sources),
            "missing_fields": list(self.missing_fields),
        }
        if self.rejected:
            record["rejected_fields"] = [
                {
                    "field_path": item.slot_name,
                    "reason_code": item.reason_code,
                }
                for item in self.rejected
            ]
        if self.unresolved_reason_code is not None:
            record["unresolved_reason_code"] = self.unresolved_reason_code
        return record


def _provenance(
    *,
    source_turn_id: str,
    match: SourceQuoteMatch,
    status: Literal["accepted", "pending"],
) -> CandidateProvenance:
    return CandidateProvenance(
        source_turn_id=source_turn_id,
        source_kind="user_explicit",
        slot_schema_version=PROCUREMENT_SLOT_SCHEMA.version,
        source_spans=((match.source_span.start, match.source_span.end),),
        validator_version=PROCUREMENT_SLOT_VALIDATOR_VERSION,
        validation_status=status,
        match_kind=match.match_kind,
    )


def _as_pending(provenance: CandidateProvenance) -> CandidateProvenance:
    return CandidateProvenance(
        source_turn_id=provenance.source_turn_id,
        source_kind=provenance.source_kind,
        slot_schema_version=provenance.slot_schema_version,
        source_spans=provenance.source_spans,
        validator_version=provenance.validator_version,
        validation_status="pending",
        match_kind=provenance.match_kind,
    )


def _normalized_contains(raw_value: str, quote: str) -> bool:
    raw = normalize_with_spans(raw_value.strip()).text
    normalized_quote = normalize_with_spans(quote).text
    return bool(raw) and raw in normalized_quote


def _all_raw_leaves_are_traceable(
    raw_value: dict[str, str | None],
    quote: str,
) -> bool:
    return all(
        value is None or _normalized_contains(value, quote)
        for value in raw_value.values()
    )


def _date_value(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _canonical_needed_year(raw_value: str) -> int | None:
    normalized = normalize_with_spans(raw_value.strip()).text
    match = _YEAR_VALUE.fullmatch(normalized)
    if match is None:
        return None
    return int(match.group("year"))


def _needed_date_raw_has_explicit_year(raw_value: object) -> bool:
    if not isinstance(raw_value, str):
        return False
    normalized = normalize_with_spans(raw_value.strip()).text
    return bool(
        _FULL_DATE.fullmatch(normalized)
        or _FULL_CN_DATE.fullmatch(normalized)
    )


def _needed_year_source_context_is_ambiguous(
    raw_value: str,
    original_quote: str,
) -> bool:
    canonical_year = _canonical_needed_year(raw_value)
    if canonical_year is None:
        return False
    normalized_quote = normalize_with_spans(original_quote).text
    distinct_years = {
        match.group(0) for match in _YEAR_TOKEN.finditer(normalized_quote)
    }
    if len(distinct_years) > 1:
        return True
    token = str(canonical_year)
    for match in re.finditer(re.escape(token), normalized_quote):
        prefix = normalized_quote[:match.start()]
        suffix = normalized_quote[match.end():]
        if _YEAR_APPROXIMATE_PREFIX.search(prefix):
            return True
        if _YEAR_APPROXIMATE_SUFFIX.search(suffix):
            return True
    return False


def _validate_needed_date(
    *,
    candidate: SlotCandidate,
    raw_value: str,
    today: date,
    provenance: CandidateProvenance,
) -> AcceptedCandidate | PendingCandidate | RejectedCandidate:
    raw = normalize_with_spans(raw_value.strip()).text
    if raw in _RELATIVE_DATES:
        value = today + timedelta(days=_RELATIVE_DATES[raw])
        return AcceptedCandidate(candidate.slot_name, value.isoformat(), provenance)
    if raw in _AMBIGUOUS_DATE_WORDS:
        return PendingCandidate(
            candidate.slot_name,
            "date_ambiguous",
            None,
            _as_pending(provenance),
        )
    match = _FULL_DATE.fullmatch(raw) or _FULL_CN_DATE.fullmatch(raw)
    if match is not None:
        value = _date_value(
            int(match.group("y")),
            int(match.group("m")),
            int(match.group("d")),
        )
        if value is None:
            return RejectedCandidate(candidate.slot_name, "date_invalid")
        if value < today:
            return RejectedCandidate(
                candidate.slot_name,
                "needed_by_date_before_today",
            )
        return AcceptedCandidate(candidate.slot_name, value.isoformat(), provenance)
    partial = _PARTIAL_DATE.fullmatch(raw)
    if partial is not None:
        month = int(partial.group("m"))
        day = int(partial.group("d"))
        if _date_value(2024, month, day) is None:
            return RejectedCandidate(candidate.slot_name, "date_invalid")
        return PendingCandidate(
            candidate.slot_name,
            "date_year_required",
            {"month": month, "day": day},
            _as_pending(provenance),
        )
    return RejectedCandidate(candidate.slot_name, "date_invalid")


def _parse_chinese_section(raw_value: str) -> int | None:
    if not raw_value:
        return None
    total = 0
    pending_digit: int | None = None
    last_unit = 10_000
    for character in raw_value:
        digit = _CHINESE_DIGITS.get(character)
        if digit is not None:
            if pending_digit not in {None, 0}:
                return None
            pending_digit = digit
            continue
        unit = _CHINESE_SMALL_UNITS.get(character)
        if unit is None or unit >= last_unit:
            return None
        if pending_digit == 0:
            return None
        coefficient = 1 if pending_digit is None else pending_digit
        total += coefficient * unit
        pending_digit = None
        last_unit = unit
    return total + (pending_digit or 0)


def _parse_chinese_integer(raw_value: str) -> int | None:
    if not raw_value or raw_value.count("万") > 1:
        return None
    if "万" not in raw_value:
        return _parse_chinese_section(raw_value)
    high, low = raw_value.split("万", 1)
    high_value = _parse_chinese_section(high)
    if high_value is None or high_value == 0:
        return None
    if not low:
        return high_value * 10_000
    low_value = _parse_chinese_section(low)
    if low_value is None or low_value >= 10_000:
        return None
    return high_value * 10_000 + low_value


def _without_money_suffix(raw_value: str) -> str:
    raw = raw_value.strip()
    for suffix in _MONEY_SUFFIXES:
        if raw.endswith(suffix):
            return raw[: -len(suffix)].strip()
    return raw


def _canonical_decimal(raw_value: str, *, quantity: bool) -> str | None:
    raw = normalize_with_spans(raw_value.strip()).text
    if not quantity:
        raw = _without_money_suffix(raw)
    if raw and all(
        character in _CHINESE_DIGITS
        or character in _CHINESE_SMALL_UNITS
        or character == "万"
        for character in raw
    ):
        chinese_value = _parse_chinese_integer(raw)
        if chinese_value is None:
            return None
        raw = str(chinese_value)
    if _DECIMAL_TEXT.fullmatch(raw) is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or (quantity and value <= 0) or (
        not quantity and value < 0
    ):
        return None
    maximum_digits = 12 if quantity else 14
    if len(value.as_tuple().digits) > maximum_digits:
        return None
    canonical = format(value, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical or "0"


def _split_quantity_measure_suffix(
    quantity: str,
    unit: str | None,
) -> tuple[str, str | None]:
    raw_quantity = quantity.strip()
    suffixes: list[str] = []
    if isinstance(unit, str) and unit.strip():
        suffixes.append(unit.strip())
    suffixes.extend(
        suffix
        for suffix in _QUANTITY_MEASURE_SUFFIXES
        if suffix not in suffixes
    )
    for suffix in suffixes:
        if not raw_quantity.endswith(suffix):
            continue
        numeric_prefix = raw_quantity[: -len(suffix)].strip()
        if _canonical_decimal(numeric_prefix, quantity=True) is not None:
            # An attached 个 is a generic classifier, not independent unit
            # evidence. Other closed suffixes are explicit current-turn text
            # and may safely populate the unit leaf after source binding.
            attached_unit = None if suffix == "个" else suffix
            return numeric_prefix, attached_unit
    return raw_quantity, None


def _same_unit(left: object, right: str) -> bool:
    if not isinstance(left, str):
        return False
    return (
        normalize_with_spans(left.strip()).text
        == normalize_with_spans(right.strip()).text
    )


def _numeric_leaf_is_source_bound(raw_value: str, quote: str) -> bool:
    raw = normalize_with_spans(raw_value.strip()).text
    normalized_quote = normalize_with_spans(quote).text
    if not raw:
        return False
    start = normalized_quote.find(raw)
    while start >= 0:
        end = start + len(raw)
        before = normalized_quote[start - 1] if start > 0 else ""
        after = normalized_quote[end] if end < len(normalized_quote) else ""
        if (
            before not in _NEGATIVE_PREFIXES
            and before not in _NUMERIC_SOURCE_CHARS
            and after not in _NUMERIC_SOURCE_CHARS
        ):
            return True
        start = normalized_quote.find(raw, start + 1)
    return False


def _numeric_leaf_has_approximate_context(raw_value: str, quote: str) -> bool:
    raw = normalize_with_spans(raw_value.strip()).text
    normalized_quote = normalize_with_spans(quote).text
    if not raw:
        return False
    start = normalized_quote.find(raw)
    while start >= 0:
        end = start + len(raw)
        prefix = normalized_quote[:start].rstrip()
        suffix = normalized_quote[end:].lstrip()
        if any(
            prefix.endswith(marker)
            for marker in _APPROXIMATE_NUMBER_PREFIXES
        ):
            return True
        for money_suffix in _MONEY_SUFFIXES:
            if suffix.startswith(money_suffix):
                suffix = suffix[len(money_suffix):].lstrip()
                break
        if any(
            suffix.startswith(marker)
            for marker in _APPROXIMATE_NUMBER_SUFFIXES
        ):
            return True
        start = normalized_quote.find(raw, start + 1)
    return False


def _quantity_leaf_has_approximate_context(
    raw_value: str,
    quote: str,
) -> bool:
    raw = normalize_with_spans(raw_value.strip()).text
    normalized_quote = normalize_with_spans(quote).text
    if not raw:
        return False
    start = normalized_quote.find(raw)
    while start >= 0:
        end = start + len(raw)
        prefix = normalized_quote[:start].rstrip()
        suffix = normalized_quote[end:].lstrip()
        if any(
            prefix.endswith(marker)
            for marker in _APPROXIMATE_NUMBER_PREFIXES
        ):
            return True
        if suffix.startswith("来"):
            return True
        if raw + suffix[:1] in _APPROXIMATE_QUANTITY_PAIRS:
            return True
        for measure_suffix in _QUANTITY_MEASURE_SUFFIXES:
            if suffix.startswith(measure_suffix):
                suffix = suffix[len(measure_suffix):].lstrip()
                break
        if any(
            suffix.startswith(marker)
            for marker in _APPROXIMATE_NUMBER_SUFFIXES
        ):
            return True
        start = normalized_quote.find(raw, start + 1)
    return False


def _generic_unit_has_independent_evidence(quote: str) -> bool:
    normalized_quote = normalize_with_spans(quote).text
    return bool(
        _EXPLICIT_GENERIC_UNIT.search(normalized_quote)
        or _STANDALONE_GENERIC_UNIT.search(normalized_quote)
    )


def _category_code(raw_value: str) -> str | None:
    normalized = normalize_with_spans(raw_value.strip()).text
    key = normalized.replace(" ", "").lower()
    return _CATEGORY_ALIASES.get(key)


def _item_field_path(item_index: int, field_name: str | None = None) -> str:
    prefix = f"items[{item_index}]"
    return prefix if field_name is None else f"{prefix}.{field_name}"


def _item_ref(source_turn_id: str, item_index: int) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"procurement-partial-item:{source_turn_id}:{item_index}",
        )
    )


def _validate_item_candidate(
    *,
    raw_value: dict[str, str | None],
    original_quote: str,
    provenance: CandidateProvenance,
    source_turn_id: str,
    item_index: int,
) -> _ValidatedItem:
    keys = set(raw_value)
    if not keys <= _ITEM_KEYS:
        rejection = RejectedCandidate(
            _item_field_path(item_index),
            "item_fields_forbidden",
        )
        return _ValidatedItem(
            item_ref=_item_ref(source_turn_id, item_index),
            fields={},
            field_sources={},
            missing_fields=_ITEM_REQUIRED,
            rejected=(rejection,),
            provenance=provenance,
        )

    fields: dict[str, object] = {}
    field_sources: dict[str, object] = {}
    rejected: list[RejectedCandidate] = []
    source = provenance.to_storage()

    for raw_name in (
        "item_name",
        "specification",
        "unit",
        "category_hint",
    ):
        raw = raw_value.get(raw_name)
        if raw is None:
            continue
        canonical_name = "category_code" if raw_name == "category_hint" else raw_name
        if not _normalized_contains(raw, original_quote):
            rejected.append(
                RejectedCandidate(
                    _item_field_path(item_index, canonical_name),
                    "raw_value_not_in_source_quote",
                )
            )
            continue
        value = raw.strip()
        if not value or len(value) > _ITEM_TEXT_LIMITS[raw_name]:
            rejected.append(
                RejectedCandidate(
                    _item_field_path(item_index, canonical_name),
                    f"item_{raw_name}_invalid",
                )
            )
            continue
        if raw_name == "category_hint":
            category = _category_code(value)
            if category is None:
                rejected.append(
                    RejectedCandidate(
                        _item_field_path(item_index, "category_code"),
                        "item_category_invalid",
                    )
                )
                continue
            fields["category_code"] = category
            field_sources["category_code"] = dict(source)
            continue
        if (
            raw_name == "unit"
            and normalize_with_spans(value).text == "个"
            and not _generic_unit_has_independent_evidence(original_quote)
        ):
            rejected.append(
                RejectedCandidate(
                    _item_field_path(item_index, "unit"),
                    "item_unit_evidence_required",
                )
            )
            continue
        fields[raw_name] = value
        field_sources[raw_name] = dict(source)

    raw_quantity = raw_value.get("quantity")
    raw_unit = raw_value.get("unit")
    if isinstance(raw_quantity, str):
        if not _normalized_contains(raw_quantity, original_quote):
            rejected.append(
                RejectedCandidate(
                    _item_field_path(item_index, "quantity"),
                    "raw_value_not_in_source_quote",
                )
            )
        else:
            normalized_quantity, attached_unit = _split_quantity_measure_suffix(
                raw_quantity,
                raw_unit if isinstance(raw_unit, str) else None,
            )
            if _quantity_leaf_has_approximate_context(
                normalized_quantity,
                original_quote,
            ):
                rejected.append(
                    RejectedCandidate(
                        _item_field_path(item_index, "quantity"),
                        "item_quantity_approximate",
                    )
                )
            elif not _numeric_leaf_is_source_bound(
                normalized_quantity,
                original_quote,
            ):
                rejected.append(
                    RejectedCandidate(
                        _item_field_path(item_index, "quantity"),
                        "item_quantity_invalid",
                    )
                )
            else:
                quantity = _canonical_decimal(
                    normalized_quantity,
                    quantity=True,
                )
                if quantity is None:
                    rejected.append(
                        RejectedCandidate(
                            _item_field_path(item_index, "quantity"),
                            "item_quantity_invalid",
                        )
                    )
                else:
                    fields["quantity"] = quantity
                    field_sources["quantity"] = dict(source)
                    if attached_unit is not None:
                        current_unit = fields.get("unit")
                        if current_unit is None:
                            fields["unit"] = attached_unit
                            field_sources["unit"] = dict(source)
                        elif not _same_unit(current_unit, attached_unit):
                            fields.pop("unit", None)
                            field_sources.pop("unit", None)
                            rejected.append(
                                RejectedCandidate(
                                    _item_field_path(item_index, "unit"),
                                    "item_unit_conflict",
                                )
                            )

    raw_price = raw_value.get("estimated_unit_price")
    if isinstance(raw_price, str):
        if not _normalized_contains(raw_price, original_quote):
            rejected.append(
                RejectedCandidate(
                    _item_field_path(item_index, "estimated_unit_price"),
                    "raw_value_not_in_source_quote",
                )
            )
        elif not _numeric_leaf_is_source_bound(raw_price, original_quote):
            rejected.append(
                RejectedCandidate(
                    _item_field_path(item_index, "estimated_unit_price"),
                    "item_price_invalid",
                )
            )
        elif _numeric_leaf_has_approximate_context(raw_price, original_quote):
            rejected.append(
                RejectedCandidate(
                    _item_field_path(item_index, "estimated_unit_price"),
                    "item_price_approximate",
                )
            )
        else:
            price = _canonical_decimal(raw_price, quantity=False)
            if price is None:
                rejected.append(
                    RejectedCandidate(
                        _item_field_path(item_index, "estimated_unit_price"),
                        "item_price_invalid",
                    )
                )
            else:
                fields["estimated_unit_price"] = price
                field_sources["estimated_unit_price"] = dict(source)

    quantity_value = fields.get("quantity")
    price_value = fields.get("estimated_unit_price")
    if (
        isinstance(quantity_value, str)
        and isinstance(price_value, str)
        and Decimal(quantity_value) * Decimal(price_value) > MAX_TOTAL_AMOUNT
    ):
        fields.pop("estimated_unit_price", None)
        field_sources.pop("estimated_unit_price", None)
        rejected.append(
            RejectedCandidate(
                _item_field_path(item_index, "estimated_unit_price"),
                "item_total_out_of_range",
            )
        )

    missing = tuple(name for name in _ITEM_REQUIRED if name not in fields)
    return _ValidatedItem(
        item_ref=_item_ref(source_turn_id, item_index),
        fields=fields,
        field_sources=field_sources,
        missing_fields=missing,
        rejected=tuple(rejected),
        provenance=provenance,
    )


def _validate_scalar_candidate(
    *,
    candidate: SlotCandidate,
    raw_value: str,
    today: date,
    provenance: CandidateProvenance,
) -> AcceptedCandidate | PendingCandidate | RejectedCandidate:
    slot_name = candidate.slot_name
    raw = raw_value.strip()
    if slot_name in _TEXT_LIMITS:
        normalized = normalize_with_spans(raw).text
        if normalized in _LABEL_ONLY_VALUES[slot_name]:
            return RejectedCandidate(slot_name, f"{slot_name}_label_only")
        if not raw:
            return RejectedCandidate(slot_name, f"{slot_name}_empty")
        if len(raw) > _TEXT_LIMITS[slot_name]:
            return RejectedCandidate(slot_name, f"{slot_name}_too_long")
        return AcceptedCandidate(slot_name, raw, provenance)
    if slot_name == "needed_by_date":
        return _validate_needed_date(
            candidate=candidate,
            raw_value=raw,
            today=today,
            provenance=provenance,
        )
    if slot_name == "needed_by_year":
        canonical_year = _canonical_needed_year(raw)
        if canonical_year is None:
            return RejectedCandidate(slot_name, "needed_by_year_invalid")
        if canonical_year < _MIN_YEAR or canonical_year > _MAX_YEAR:
            return RejectedCandidate(slot_name, "needed_by_year_out_of_range")
        return AcceptedCandidate(slot_name, canonical_year, provenance)
    if slot_name == "currency":
        if raw.lower() == "cny" or raw == "人民币":
            return AcceptedCandidate(slot_name, "CNY", provenance)
        return RejectedCandidate(slot_name, "currency_invalid")
    if slot_name in {"request_id", "task_id"}:
        try:
            value = str(UUID(raw))
        except ValueError:
            return RejectedCandidate(slot_name, f"{slot_name}_invalid")
        return AcceptedCandidate(slot_name, value, provenance)
    return RejectedCandidate(slot_name, "slot_not_supported")


def _canonical_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _combined_item_candidate(
    candidates: list[AcceptedCandidate],
) -> AcceptedCandidate:
    first = candidates[0].provenance
    spans = tuple(
        span
        for candidate in candidates
        for span in candidate.provenance.source_spans
    )
    match_kind: Literal["original_exact", "controlled_normalized_exact"] = (
        "controlled_normalized_exact"
        if any(
            candidate.provenance.match_kind == "controlled_normalized_exact"
            for candidate in candidates
        )
        else "original_exact"
    )
    provenance = CandidateProvenance(
        source_turn_id=first.source_turn_id,
        source_kind=first.source_kind,
        slot_schema_version=first.slot_schema_version,
        source_spans=spans,
        validator_version=first.validator_version,
        validation_status="accepted",
        match_kind=match_kind,
    )
    return AcceptedCandidate(
        "items",
        [candidate.canonical_value for candidate in candidates],
        provenance,
    )


def validate_procurement_candidates(
    *,
    text: str,
    envelope: SlotExtractionEnvelope,
    today: date,
    source_turn_id: str,
) -> ProcurementCandidateValidationResult:
    validate_envelope_for_module(envelope, PROCUREMENT_SLOT_SCHEMA)
    accepted_groups: dict[str, list[AcceptedCandidate]] = defaultdict(list)
    pending_groups: dict[str, list[PendingCandidate]] = defaultdict(list)
    rejected: list[RejectedCandidate] = []
    item_results: list[_ValidatedItem] = []
    item_index = 0
    ambiguous_item_sources = 0
    redundant_needed_year = any(
        candidate.slot_name == "needed_by_date"
        and _needed_date_raw_has_explicit_year(candidate.raw_value)
        for candidate in envelope.candidates
    )

    for candidate in envelope.candidates:
        if candidate.slot_name == "needed_by_year" and redundant_needed_year:
            continue
        current_item_index = item_index
        if candidate.slot_name == "items":
            item_index += 1
        try:
            match = locate_source_quote(text, candidate.source_quote)
        except SourceQuoteNotFound:
            rejection = RejectedCandidate(
                _item_field_path(current_item_index)
                if candidate.slot_name == "items"
                else candidate.slot_name,
                "source_quote_not_found",
            )
            rejected.append(rejection)
            if candidate.slot_name == "items":
                item_results.append(_ValidatedItem(
                    item_ref=_item_ref(source_turn_id, current_item_index),
                    fields={},
                    field_sources={},
                    missing_fields=_ITEM_REQUIRED,
                    rejected=(rejection,),
                    provenance=None,
                ))
            continue
        except SourceQuoteAmbiguous:
            if candidate.slot_name == "items":
                ambiguous_item_sources += 1
                item_results.append(_ValidatedItem(
                    item_ref=_item_ref(source_turn_id, current_item_index),
                    fields={},
                    field_sources={},
                    missing_fields=_ITEM_REQUIRED,
                    rejected=(),
                    provenance=None,
                    unresolved_reason_code="source_quote_ambiguous",
                ))
                continue
            pending_groups[candidate.slot_name].append(
                PendingCandidate(
                    candidate.slot_name,
                    "source_quote_ambiguous",
                    None,
                    None,
                )
            )
            continue
        original_quote = text[match.source_span.start : match.source_span.end]
        provenance = _provenance(
            source_turn_id=source_turn_id,
            match=match,
            status="accepted",
        )
        raw_value = candidate.raw_value
        if isinstance(raw_value, str):
            if not _normalized_contains(raw_value, original_quote):
                rejected.append(
                    RejectedCandidate(
                        candidate.slot_name,
                        "raw_value_not_in_source_quote",
                    )
                )
                continue
            if (
                candidate.slot_name == "needed_by_year"
                and _needed_year_source_context_is_ambiguous(
                    raw_value,
                    original_quote,
                )
            ):
                rejected.append(
                    RejectedCandidate(
                        "needed_by_year",
                        "needed_by_year_invalid",
                    )
                )
                continue
            classified = _validate_scalar_candidate(
                candidate=candidate,
                raw_value=raw_value,
                today=today,
                provenance=provenance,
            )
        else:
            item_result = _validate_item_candidate(
                raw_value=dict(raw_value),
                original_quote=original_quote,
                provenance=provenance,
                source_turn_id=source_turn_id,
                item_index=current_item_index,
            )
            item_results.append(item_result)
            rejected.extend(item_result.rejected)
            continue
        if isinstance(classified, AcceptedCandidate):
            accepted_groups[candidate.slot_name].append(classified)
        elif isinstance(classified, PendingCandidate):
            pending_groups[candidate.slot_name].append(classified)
        else:
            rejected.append(classified)

    accepted: dict[str, AcceptedCandidate] = {}
    pending: dict[str, PendingCandidate] = {}
    complete_item_candidates = [
        AcceptedCandidate(
            "items",
            item.canonical_item(),
            item.provenance,
        )
        for item in item_results
        if item.complete and item.provenance is not None
    ]
    has_persistable_item_state = any(
        item.complete
        or bool(item.fields)
        or item.unresolved_reason_code is not None
        for item in item_results
    )
    unresolved_items = [
        item
        for item in item_results
        if not item.complete
        and (
            bool(item.fields)
            or item.unresolved_reason_code is not None
            or (bool(item.rejected) and has_persistable_item_state)
        )
    ]
    if complete_item_candidates:
        accepted["items"] = _combined_item_candidate(complete_item_candidates)
    if unresolved_items or ambiguous_item_sources:
        first_source = next(
            (
                _as_pending(item.provenance)
                for item in unresolved_items
                if item.provenance is not None
            ),
            None,
        )
        fragment = (
            {"items": [item.pending_record() for item in unresolved_items]}
            if unresolved_items
            else None
        )
        pending["items"] = PendingCandidate(
            "items",
            (
                "source_quote_ambiguous"
                if ambiguous_item_sources
                else "item_fields_required"
            ),
            fragment,
            first_source,
        )

    for slot_name in set(accepted_groups) | set(pending_groups):
        candidates = accepted_groups.get(slot_name, [])
        pending_candidates = pending_groups.get(slot_name, [])
        values = {_canonical_key(candidate.canonical_value) for candidate in candidates}
        if len(values) > 1 or (candidates and pending_candidates) or len(
            pending_candidates
        ) > 1:
            source = (
                _as_pending(candidates[0].provenance)
                if candidates
                else pending_candidates[0].provenance
            )
            pending[slot_name] = PendingCandidate(
                slot_name,
                "slot_value_ambiguous",
                None,
                source,
            )
        elif candidates:
            accepted[slot_name] = candidates[0]
        elif pending_candidates:
            pending[slot_name] = pending_candidates[0]

    return CandidateValidationResult(
        accepted=accepted,
        pending=pending,
        rejected=tuple(rejected),
    )


__all__ = [
    "PROCUREMENT_SLOT_VALIDATOR_VERSION",
    "ProcurementCandidateValidationResult",
    "ProcurementItemRaw",
    "validate_procurement_candidates",
]
