from datetime import datetime, timezone
from uuid import uuid4

from policy_api.assistant_drafts.store import DraftSnapshot
from policy_api.hr.draft import HrDraftMerge
from policy_api.hr.runtime import HrRuntime, _with_hr_draft_guidance
from policy_api.slot_extraction.merge import (
    CandidateValidationResult,
    RejectedCandidate,
)
from policy_api.tools.schemas import (
    AssistantDraftBlock,
    ClarificationBlock,
    TextBlock,
    ToolTurnResponse,
)


def _snapshot() -> DraftSnapshot:
    return DraftSnapshot(
        id=uuid4(),
        owner_user_id=uuid4(),
        module_key="hr",
        conversation_id=uuid4(),
        intent="submit_leave",
        status="active",
        version=1,
        fields={"leave_type_code": "annual", "reason": "探亲"},
        sources={},
        pending={"date_range": {"reason_code": "date_year_required"}},
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )


def test_hr_clarification_never_exposes_validator_reason_codes_as_suggestions() -> None:
    response = HrRuntime._hr_draft_clarification_response(
        _snapshot(),
        HrDraftMerge(
            fields={"leave_type_code": "annual", "reason": "探亲"},
            pending={"date_range": {"reason_code": "date_year_required"}},
            sources={},
            missing_fields=("year",),
        ),
        CandidateValidationResult(
            accepted={},
            pending={},
            rejected=(RejectedCandidate("year", "year_invalid"),),
        ),
        slot_extraction_calls=1,
    )

    clarification = next(
        block for block in response.blocks
        if isinstance(block, ClarificationBlock)
    )
    text = next(block.text for block in response.blocks if isinstance(block, TextBlock))
    assert clarification.suggestions == ()
    assert "year_invalid" not in text
    assert "year:year_invalid" not in text
    assert "请假年份" in text


def test_hr_year_required_date_projects_only_the_year_as_actionable() -> None:
    response = HrRuntime._hr_draft_clarification_response(
        _snapshot(),
        HrDraftMerge(
            fields={"leave_type_code": "annual", "reason": "探亲"},
            pending={"date_range": {"reason_code": "date_year_required"}},
            sources={},
            missing_fields=("year",),
        ),
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
        "已记录本会话中你明确提供的请假信息，请只补充：请假年份。"
        "无需重复已经提供的内容。"
    )
    assert clarification.missing_fields == ("year",)
    assert draft.pending_fields == ()
    assert draft.missing_fields == ("year",)


def test_hr_pending_date_range_uses_user_facing_date_label() -> None:
    response = HrRuntime._hr_draft_clarification_response(
        _snapshot(),
        HrDraftMerge(
            fields={"leave_type_code": "annual", "reason": "探亲", "year": 2026},
            pending={"date_range": {"reason_code": "date_ambiguous"}},
            sources={},
            missing_fields=(),
        ),
        CandidateValidationResult(accepted={}, pending={}, rejected=()),
        slot_extraction_calls=1,
    )

    text = next(block.text for block in response.blocks if isinstance(block, TextBlock))
    assert "请假日期" in text
    assert "date_range" not in text


def test_hr_post_planner_missing_guidance_hides_unknown_field_name() -> None:
    sentinel = "legacy_hr_post_planner_missing"
    response = _with_hr_draft_guidance(
        ToolTurnResponse(
            blocks=(TextBlock(text="请补充信息。"),),
            model_calls=1,
            read_calls=0,
            write_proposals=0,
            slot_extraction_calls=1,
        ),
        HrDraftMerge(
            fields={},
            pending={},
            sources={},
            missing_fields=(sentinel,),
        ),
    )

    text = next(block.text for block in response.blocks if isinstance(block, TextBlock))
    assert sentinel not in text
    assert "待补充信息" in text


def test_hr_deterministic_missing_guidance_hides_unknown_field_name() -> None:
    sentinel = "legacy_hr_deterministic_missing"
    response = HrRuntime._hr_draft_clarification_response(
        _snapshot(),
        HrDraftMerge(
            fields={"leave_type_code": "annual", "reason": "探亲"},
            pending={},
            sources={},
            missing_fields=(sentinel,),
        ),
        CandidateValidationResult(accepted={}, pending={}, rejected=()),
        slot_extraction_calls=1,
    )

    text = next(block.text for block in response.blocks if isinstance(block, TextBlock))
    clarification = next(
        block for block in response.blocks if isinstance(block, ClarificationBlock)
    )
    assert sentinel not in text
    assert "待补充信息" in text
    assert clarification.missing_fields == (sentinel,)


def test_hr_deterministic_pending_guidance_hides_unknown_field_name() -> None:
    sentinel = "legacy_hr_deterministic_pending"
    response = HrRuntime._hr_draft_clarification_response(
        _snapshot(),
        HrDraftMerge(
            fields={"leave_type_code": "annual", "reason": "探亲", "year": 2026},
            pending={sentinel: {"reason_code": "legacy_pending"}},
            sources={},
            missing_fields=(),
        ),
        CandidateValidationResult(accepted={}, pending={}, rejected=()),
        slot_extraction_calls=1,
    )

    text = next(block.text for block in response.blocks if isinstance(block, TextBlock))
    clarification = next(
        block for block in response.blocks if isinstance(block, ClarificationBlock)
    )
    assert sentinel not in text
    assert "待确认信息" in text
    assert clarification.missing_fields == (sentinel,)
