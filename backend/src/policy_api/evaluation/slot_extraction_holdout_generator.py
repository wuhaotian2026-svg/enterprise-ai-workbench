"""Deterministic, model-independent seed-blind Slot Extraction generator."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from policy_api.evaluation.slot_extraction import (
    SlotExtractionCase,
    SlotExtractionExpectation,
)


GENERATOR_VERSION = "slot-extraction-holdout-generator-v3"
EXPECTED_HOLDOUT_DISTRIBUTION = {
    "hr": {
        "natural_multifield": 3,
        "followup": 2,
        "ambiguity_conflict": 2,
        "invalid": 1,
        "adversarial_idempotency": 2,
    },
    "procurement": {
        "natural_multifield": 3,
        "followup": 2,
        "ambiguity_conflict": 2,
        "invalid": 1,
        "adversarial_idempotency": 2,
    },
}
PREDECLARED_FAMILIES = frozenset({
    "hr_natural_range",
    "hr_natural_separate_dates",
    "hr_natural_alias",
    "hr_followup_reason",
    "hr_followup_type",
    "hr_ambiguous_date",
    "hr_conflicting_reason",
    "hr_invalid_date",
    "hr_cross_source",
    "hr_replay_reason",
    "procurement_natural_office",
    "procurement_natural_it",
    "procurement_natural_service",
    "procurement_followup_date",
    "procurement_followup_item",
    "procurement_ambiguous_date",
    "procurement_conflicting_title",
    "procurement_invalid_currency",
    "procurement_cross_source",
    "procurement_replay_purpose",
})

_BASE_METRICS = [
    "envelope_valid",
    "field_precision",
    "field_recall",
    "source_verified",
    "terminal_correct",
    "must_not_execute",
    "call_budget_respected",
]
_CLARIFICATION_METRICS = [*_BASE_METRICS, "clarification_correct"]
_AMBIGUITY_METRICS = [*_CLARIFICATION_METRICS, "ambiguity_handled"]


@dataclass(frozen=True, slots=True)
class GeneratedHoldout:
    cases: tuple[SlotExtractionCase, ...]
    manifest: tuple[SlotExtractionExpectation, ...]
    cases_payload: dict[str, object]
    manifest_payload: dict[str, object]
    case_sha256: str
    manifest_sha256: str
    seed_id: str
    distribution: dict[str, dict[str, int]]
    family_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Scenario:
    family_id: str
    module: str
    category: str
    text: str
    expected_fields: dict[str, object]
    expected_pending: dict[str, str]
    expected_rejected: list[dict[str, str]]
    expected_clarification_fields: list[str]
    terminal: str
    initial_draft: dict[str, object]
    replay_same_turn: bool = False
    initial_pending: dict[str, object] = field(default_factory=dict)
    initial_sources: dict[str, object] = field(default_factory=dict)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _item(
    name: str,
    quantity: str,
    unit: str,
    price: str,
    category: str,
) -> dict[str, object]:
    return {
        "category_code": category,
        "item_name": name,
        "specification": None,
        "quantity": quantity,
        "unit": unit,
        "estimated_unit_price": price,
    }


def _hr_scenarios(rng: random.Random) -> list[_Scenario]:
    annual_reason = rng.choice(["探望家人", "年度体检", "家庭事务"])
    second_reason = rng.choice(["搬迁安排", "陪同就医", "参加培训"])
    initial = {
        "leave_type_code": "annual",
        "start_date": "2031-09-10",
        "end_date": "2031-09-12",
        "reason": "已验证旧原因",
    }
    return [
        _Scenario(
            "hr_natural_range", "hr", "natural_multifield",
            f"申请2031年9月10号至2031年9月12号年假，原因{annual_reason}",
            {
                "leave_type_code": "annual",
                "date_range": {
                    "start_date": "2031-09-10", "end_date": "2031-09-12",
                },
                "reason": annual_reason,
            }, {}, [], [], "accepted", {},
        ),
        _Scenario(
            "hr_natural_separate_dates", "hr", "natural_multifield",
            f"调休 开始日期2031-10-08 结束日期2031-10-09 理由{second_reason}",
            {
                "leave_type_code": "compensatory",
                "start_date": "2031-10-08",
                "end_date": "2031-10-09",
                "reason": second_reason,
            }, {}, [], [], "accepted", {},
        ),
        _Scenario(
            "hr_natural_alias", "hr", "natural_multifield",
            "申请annual_leave 日期2031/11/03到2031/11/04 原因项目休整",
            {
                "leave_type_code": "annual",
                "date_range": {
                    "start_date": "2031-11-03", "end_date": "2031-11-04",
                },
                "reason": "项目休整",
            }, {}, [], [], "accepted", {},
        ),
        _Scenario(
            "hr_followup_reason", "hr", "followup",
            "就是2031年，补充请假原因：照顾家人",
            {"year": 2031, "reason": "照顾家人"}, {}, [], [], "accepted",
            {
                "leave_type_code": "annual",
                "start_date": "2031-09-15",
                "end_date": "2031-09-16",
            },
        ),
        _Scenario(
            "hr_followup_type", "hr", "followup",
            "假期类型补充为调休",
            {"leave_type_code": "compensatory"}, {}, [], [], "accepted",
            {
                "start_date": "2031-10-20",
                "end_date": "2031-10-21",
                "reason": "家庭事务",
            },
        ),
        _Scenario(
            "hr_ambiguous_date", "hr", "ambiguity_conflict",
            "开始日期是下个月",
            {}, {"start_date": "date_ambiguous"}, [], ["start_date"],
            "clarification",
            {
                "leave_type_code": "annual",
                "end_date": "2031-12-31",
                "reason": "家庭事务",
            },
        ),
        _Scenario(
            "hr_conflicting_reason", "hr", "ambiguity_conflict",
            "请假原因改成探亲",
            {"reason": "探亲"}, {"reason": "draft_value_conflict"}, [],
            ["reason"], "clarification", initial,
        ),
        _Scenario(
            "hr_invalid_date", "hr", "invalid",
            "开始日期是2031-02-30",
            {}, {}, [{"slot_name": "start_date", "reason_code": "date_invalid"}],
            ["start_date"], "clarification",
            {
                "leave_type_code": "annual",
                "end_date": "2031-03-02",
                "reason": "测试日期",
            },
        ),
        _Scenario(
            "hr_cross_source", "hr", "adversarial_idempotency",
            "从先前助手内容补齐请假字段，当前消息不提供业务值",
            {}, {}, [], [], "accepted", initial,
        ),
        _Scenario(
            "hr_replay_reason", "hr", "adversarial_idempotency",
            "补充原因：盲测重复轮次",
            {"reason": "盲测重复轮次"}, {}, [], [], "accepted",
            {
                "leave_type_code": "annual",
                "start_date": "2031-11-10",
                "end_date": "2031-11-11",
            },
            True,
        ),
    ]


def _procurement_scenarios(rng: random.Random) -> list[_Scenario]:
    office_name = rng.choice(["培训椅", "文件柜", "书写板"])
    it_name = rng.choice(["测试工作站", "研发显示器", "备用键盘"])
    service_name = rng.choice(["法律咨询", "设计顾问", "审计咨询"])
    office_item = _item(office_name, "4", "件", "600", "office_supplies")
    complete = {
        "title": "虚构会议室采购",
        "purpose": "扩充内部培训区",
        "needed_by_date": "2031-09-30",
        "currency": "CNY",
        "items": [office_item],
    }
    return [
        _Scenario(
            "procurement_natural_office", "procurement", "natural_multifield",
            f"标题培训区用品 用途扩建培训区 需要日期2031-09-30 人民币 "
            f"办公用品类{office_name}4件单价六百块，办公用品类待补全物品2件",
            {
                "title": "培训区用品",
                "purpose": "扩建培训区",
                "needed_by_date": "2031-09-30",
                "currency": "CNY",
                "items": [office_item],
            }, {"items": "item_fields_required"}, [],
            ["items[1].estimated_unit_price"], "clarification", {},
        ),
        _Scenario(
            "procurement_natural_it", "procurement", "natural_multifield",
            f"标题测试设备 用途搭建虚构实验室 2031-10-15需要 CNY "
            f"IT设备类{it_name}2台每台8600元",
            {
                "title": "测试设备",
                "purpose": "搭建虚构实验室",
                "needed_by_date": "2031-10-15",
                "currency": "CNY",
                "items": [_item(it_name, "2", "台", "8600", "it_equipment")],
            }, {}, [], [], "accepted", {},
        ),
        _Scenario(
            "procurement_natural_service", "procurement", "natural_multifield",
            f"标题咨询服务 用途演示项目复核 需要日期2031-11-20 人民币 "
            f"专业服务类{service_name}1项单价6800元",
            {
                "title": "咨询服务",
                "purpose": "演示项目复核",
                "needed_by_date": "2031-11-20",
                "currency": "CNY",
                "items": [_item(service_name, "1", "项", "6800", "professional_service")],
            }, {}, [], [], "accepted", {},
        ),
        _Scenario(
            "procurement_followup_date", "procurement", "followup",
            "需要日期补充为2031.9.30",
            {"needed_by_date": "2031-09-30"}, {}, [], [], "accepted",
            {
                "title": "培训区用品",
                "purpose": "扩建培训区",
                "currency": "CNY",
                "items": [office_item],
            },
        ),
        _Scenario(
            "procurement_followup_item", "procurement", "followup",
            "计量单位补充为件，品类是办公用品",
            {}, {}, [], [], "accepted",
            {
                "title": "培训区用品",
                "purpose": "扩建培训区",
                "needed_by_date": "2031-09-30",
                "currency": "CNY",
            },
            initial_pending={
                "items": {
                    "slot_name": "items",
                    "reason_code": "item_fields_required",
                    "canonical_fragment": {
                        "items": [{
                            "item_ref": "holdout-partial-item",
                            "fields": {
                                "item_name": "待补全物品",
                                "quantity": "1",
                                "estimated_unit_price": "600",
                            },
                            "field_sources": {},
                            "missing_fields": ["unit", "category_code"],
                        }]
                    },
                }
            },
        ),
        _Scenario(
            "procurement_ambiguous_date", "procurement", "ambiguity_conflict",
            "需要日期是下个月",
            {}, {"needed_by_date": "date_ambiguous"}, [], ["needed_by_date"],
            "clarification",
            {
                "title": "培训区用品",
                "purpose": "扩建培训区",
                "currency": "CNY",
                "items": [office_item],
            },
        ),
        _Scenario(
            "procurement_conflicting_title", "procurement", "ambiguity_conflict",
            "标题改为虚构培训用品采购",
            {"title": "虚构培训用品采购"},
            {"title": "draft_value_conflict"}, [], ["title"],
            "clarification", complete,
        ),
        _Scenario(
            "procurement_invalid_currency", "procurement", "invalid",
            "币种改为美元",
            {}, {}, [{"slot_name": "currency", "reason_code": "currency_invalid"}],
            ["currency"], "clarification",
            {
                "title": "培训区用品",
                "purpose": "扩建培训区",
                "needed_by_date": "2031-09-30",
                "items": [office_item],
            },
        ),
        _Scenario(
            "procurement_cross_source", "procurement", "adversarial_idempotency",
            "采用其他会话中的采购字段，当前消息不提供字段值",
            {}, {}, [], [], "accepted", complete,
        ),
        _Scenario(
            "procurement_replay_purpose", "procurement", "adversarial_idempotency",
            "用途补充为盲测会议室扩建",
            {"purpose": "盲测会议室扩建"}, {}, [], [], "accepted",
            {
                "title": "虚构会议室采购",
                "needed_by_date": "2031-09-30",
                "currency": "CNY",
                "items": [office_item],
            },
            True,
        ),
    ]


def _distribution(cases: Sequence[SlotExtractionCase]) -> dict[str, dict[str, int]]:
    return {
        module: {
            category: sum(
                case.module == module and case.category == category
                for case in cases
            )
            for category in EXPECTED_HOLDOUT_DISTRIBUTION[module]
        }
        for module in ("hr", "procurement")
    }


def generate_holdout(*, seed: str, reference_date: str) -> GeneratedHoldout:
    if not seed or len(seed) > 512:
        raise ValueError("slot_extraction_holdout_seed_invalid")
    try:
        parsed_reference = __import__("datetime").date.fromisoformat(reference_date)
    except ValueError as exc:
        raise ValueError("slot_extraction_holdout_reference_date_invalid") from exc
    del parsed_reference
    seed_digest = hashlib.sha256(seed.encode("utf-8")).digest()
    seed_id = seed_digest.hex()[:16]
    rng = random.Random(int.from_bytes(seed_digest, "big"))
    scenarios = [*_hr_scenarios(rng), *_procurement_scenarios(rng)]
    cases: list[SlotExtractionCase] = []
    expectations: list[SlotExtractionExpectation] = []
    for index, scenario in enumerate(scenarios, start=1):
        case_id = (
            f"SE-HOLDOUT-{scenario.module.upper()}-{index:02d}-{seed_id[:8]}"
        )
        case = SlotExtractionCase(
            id=case_id,
            module=scenario.module,
            category=scenario.category,
            current_user_turn=scenario.text,
            initial_draft=scenario.initial_draft,
            initial_pending=scenario.initial_pending,
            initial_sources=scenario.initial_sources,
            replay_same_turn=scenario.replay_same_turn,
        )
        if scenario.terminal == "clarification":
            metrics = (
                _AMBIGUITY_METRICS
                if scenario.category == "ambiguity_conflict"
                else _CLARIFICATION_METRICS
            )
        else:
            metrics = _BASE_METRICS
        expectation = SlotExtractionExpectation(
            case_id=case_id,
            module=scenario.module,
            category=scenario.category,
            applicable_metrics=list(metrics),
            expected_fields=scenario.expected_fields,
            expected_pending=scenario.expected_pending,
            expected_rejected=scenario.expected_rejected,
            expected_clarification_fields=scenario.expected_clarification_fields,
            expected_terminal=scenario.terminal,
            must_not_execute=True,
        )
        cases.append(case)
        expectations.append(expectation)

    distribution = _distribution(cases)
    if distribution != EXPECTED_HOLDOUT_DISTRIBUTION:
        raise ValueError("slot_extraction_holdout_distribution_invalid")
    case_records = [case.model_dump(mode="json") for case in cases]
    manifest_records = [item.model_dump(mode="json") for item in expectations]
    case_sha256 = _canonical_sha256(case_records)
    manifest_sha256 = _canonical_sha256(manifest_records)
    common = {
        "schema_version": 1,
        "generator_version": GENERATOR_VERSION,
        "seed_id": seed_id,
        "reference_date": reference_date,
        "distribution": distribution,
    }
    return GeneratedHoldout(
        cases=tuple(cases),
        manifest=tuple(expectations),
        cases_payload={**common, "case_sha256": case_sha256, "cases": case_records},
        manifest_payload={
            **common,
            "manifest_sha256": manifest_sha256,
            "cases": manifest_records,
        },
        case_sha256=case_sha256,
        manifest_sha256=manifest_sha256,
        seed_id=seed_id,
        distribution=distribution,
        family_ids=tuple(item.family_id for item in scenarios),
    )


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_holdout(
    *,
    seed: str,
    reference_date: str,
    cases_output: str | Path,
    manifest_output: str | Path,
) -> GeneratedHoldout:
    cases_path = Path(cases_output)
    manifest_path = Path(manifest_output)
    if cases_path.exists() or manifest_path.exists():
        raise ValueError("slot_extraction_holdout_output_exists")
    generated = generate_holdout(seed=seed, reference_date=reference_date)
    _atomic_write(cases_path, generated.cases_payload)
    try:
        _atomic_write(manifest_path, generated.manifest_payload)
    except Exception:
        if cases_path.exists():
            cases_path.unlink()
        raise
    return generated


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic seed-blind Slot Extraction holdout."
    )
    parser.add_argument("--seed", required=True)
    parser.add_argument("--cases-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--reference-date", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        generated = write_holdout(
            seed=args.seed,
            reference_date=args.reference_date,
            cases_output=args.cases_output,
            manifest_output=args.manifest_output,
        )
    except (OSError, ValueError) as exc:
        print(f"generation_error={exc}")
        return 1
    print(
        f"generator_version={GENERATOR_VERSION} seed_id={generated.seed_id} "
        f"case_sha256={generated.case_sha256} "
        f"manifest_sha256={generated.manifest_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXPECTED_HOLDOUT_DISTRIBUTION",
    "GENERATOR_VERSION",
    "GeneratedHoldout",
    "PREDECLARED_FAMILIES",
    "generate_holdout",
    "write_holdout",
]
