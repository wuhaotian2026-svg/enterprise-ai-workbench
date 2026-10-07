from policy_api.hr.runtime import hr_text_postcondition


def test_internal_planning_is_not_returned_to_user() -> None:
    leaked = (
        "I need to clarify the missing required fields before proceeding. "
        "Let me reconsider fact_flags and collected_argument_names."
    )

    assert hr_text_postcondition(leaked) == "请补充或确认本次请假所需的信息。"


def test_normal_concise_chinese_reply_is_preserved() -> None:
    text = "请确认请假日期是否为 2026-09-01 至 2026-09-05。"

    assert hr_text_postcondition(text) == text
