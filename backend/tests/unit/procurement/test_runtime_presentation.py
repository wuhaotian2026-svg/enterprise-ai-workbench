from datetime import datetime, timezone
import logging
from uuid import uuid4

import pytest

from policy_api.assistant_drafts.store import DraftSnapshot
from policy_api.procurement.draft import (
    ProcurementDraftMerge,
    project_procurement_clarification_fields,
)
from policy_api.procurement.runtime import ProcurementRuntime
from policy_api.slot_extraction.merge import CandidateValidationResult
from policy_api.tools.schemas import AssistantDraftBlock, ClarificationBlock, TextBlock


def test_non_leaf_item_paths_do_not_cover_parent_pending_item() -> None:
    pending = {"title": {}, "items": {}, "purpose": {}}

    for item_path in ("items[0]", "items[x]", "items[0]."):
        missing_fields = ("currency", item_path, "needed_by_date")

        projected = project_procurement_clarification_fields(
            missing_fields=missing_fields,
            pending=pending,
        )

        assert projected == (
            *missing_fields,
            "items",
            "purpose",
            "title",
        )


def test_partial_item_missing_paths_are_grouped_into_user_facing_guidance() -> None:
    partial_item = {
        "item_ref": "partial-1",
        "fields": {
            "item_name": "桌子",
            "quantity": "1",
            "estimated_unit_price": "600",
        },
        "field_sources": {},
        "missing_fields": ["unit", "category_code"],
    }
    pending = {
        "items": {
            "slot_name": "items",
            "reason_code": "item_fields_required",
            "canonical_fragment": {"items": [partial_item]},
        }
    }
    snapshot = DraftSnapshot(
        id=uuid4(),
        owner_user_id=uuid4(),
        module_key="procurement",
        conversation_id=uuid4(),
        intent="draft_request",
        status="active",
        version=1,
        fields={"title": "办公用品"},
        sources={},
        pending=pending,
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    merge = ProcurementDraftMerge(
        fields={"title": "办公用品"},
        pending=pending,
        sources={},
        missing_fields=(
            "purpose",
            "needed_by_date",
            "currency",
            "items[0].unit",
            "items[0].category_code",
        ),
    )

    response = ProcurementRuntime._procurement_draft_response(
        snapshot,
        merge,
        CandidateValidationResult(accepted={}, pending={}, rejected=()),
        slot_extraction_calls=1,
    )

    text = next(block.text for block in response.blocks if isinstance(block, TextBlock))
    clarification = next(
        block for block in response.blocks
        if isinstance(block, ClarificationBlock)
    )
    assert "第 1 项（桌子）的计量单位、品类" in text
    assert "items[0].unit" not in text
    assert "items[0].category_code" not in text
    assert clarification.missing_fields == merge.missing_fields
    assert clarification.suggestions == ()


def test_whole_item_path_has_actionable_user_facing_guidance() -> None:
    pending = {"items": {"reason_code": "source_quote_ambiguous"}}
    snapshot = DraftSnapshot(
        id=uuid4(),
        owner_user_id=uuid4(),
        module_key="procurement",
        conversation_id=uuid4(),
        intent="draft_request",
        status="active",
        version=1,
        fields={"title": "办公用品"},
        sources={},
        pending=pending,
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    merge = ProcurementDraftMerge(
        fields={"title": "办公用品"},
        pending=pending,
        sources={},
        missing_fields=("items[0]",),
    )

    response = ProcurementRuntime._procurement_draft_response(
        snapshot,
        merge,
        CandidateValidationResult(accepted={}, pending={}, rejected=()),
        slot_extraction_calls=1,
    )

    text = next(block.text for block in response.blocks if isinstance(block, TextBlock))
    assert "第 1 项采购明细" in text
    assert "待补充信息" not in text
    assert "items[0]" not in text


@pytest.mark.parametrize(
    ("sentinel", "expected_path_kind"),
    [
        ("legacy_procurement_pending", "top_level_unknown"),
        ("items[x]", "item_path_malformed"),
        ("items[0].future_leaf", "item_leaf_unknown"),
    ],
)
def test_procurement_pending_guidance_fails_closed_for_unknown_field(
    caplog: pytest.LogCaptureFixture,
    sentinel: str,
    expected_path_kind: str,
) -> None:
    pending = {sentinel: {"reason_code": "legacy_pending"}}
    snapshot = DraftSnapshot(
        id=uuid4(),
        owner_user_id=uuid4(),
        module_key="procurement",
        conversation_id=uuid4(),
        intent="draft_request",
        status="active",
        version=1,
        fields={"title": "办公用品"},
        sources={},
        pending=pending,
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    merge = ProcurementDraftMerge(
        fields={"title": "办公用品"},
        pending=pending,
        sources={},
        missing_fields=(),
    )

    with caplog.at_level(logging.WARNING, logger="policy_api.procurement.runtime"):
        response = ProcurementRuntime._procurement_draft_response(
            snapshot,
            merge,
            CandidateValidationResult(accepted={}, pending={}, rejected=()),
            slot_extraction_calls=1,
        )

    text = next(block.text for block in response.blocks if isinstance(block, TextBlock))
    clarification = next(
        block for block in response.blocks if isinstance(block, ClarificationBlock)
    )
    assert sentinel not in text
    assert "草稿中存在无法识别的信息状态，请清空草稿后重试" in text
    assert "待确认信息" not in text
    assert "待补充信息" not in text
    assert clarification.missing_fields == (sentinel,)
    records = [
        record
        for record in caplog.records
        if record.getMessage() == "procurement_missing_path_unknown"
    ]
    assert len(records) == 1
    assert records[0].path_kind == expected_path_kind
    assert sentinel not in records[0].getMessage()


def test_procurement_year_required_date_projects_only_needed_year_as_actionable() -> None:
    pending = {
        "needed_by_date": {
            "slot_name": "needed_by_date",
            "reason_code": "date_year_required",
            "canonical_fragment": {"month": 9, "day": 20},
        }
    }
    fields = {
        "title": "办公用品",
        "purpose": "办公室",
        "currency": "CNY",
        "items": [{
            "category_code": "office_supplies",
            "item_name": "笔",
            "specification": None,
            "quantity": "20",
            "unit": "支",
            "estimated_unit_price": "5",
        }],
    }
    snapshot = DraftSnapshot(
        id=uuid4(),
        owner_user_id=uuid4(),
        module_key="procurement",
        conversation_id=uuid4(),
        intent="draft_request",
        status="active",
        version=2,
        fields=fields,
        sources={},
        pending=pending,
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    merge = ProcurementDraftMerge(
        fields=fields,
        pending=pending,
        sources={},
        missing_fields=("needed_by_date",),
    )

    response = ProcurementRuntime._procurement_draft_response(
        snapshot,
        merge,
        CandidateValidationResult(accepted={}, pending={}, rejected=()),
        slot_extraction_calls=1,
    )

    text = next(block.text for block in response.blocks if isinstance(block, TextBlock))
    clarification = next(
        block for block in response.blocks if isinstance(block, ClarificationBlock)
    )
    draft = next(
        block for block in response.blocks if isinstance(block, AssistantDraftBlock)
    )

    assert text == (
        "已记录本会话中你明确提供的采购信息，请只补充：需要日期年份。"
        "无需重复已经提供的内容。"
    )
    assert clarification.missing_fields == ("needed_by_year",)
    assert draft.pending_fields == ()
    assert draft.missing_fields == ("needed_by_year",)
