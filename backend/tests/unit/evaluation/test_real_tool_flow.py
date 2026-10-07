from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

import pytest

from policy_api.evaluation.real_tool_flow import (
    EvaluationFixtureStore,
    EvaluationWriteProposer,
    RecordingPlanner,
    SafeRealToolFlowEvaluator,
    build_safe_evaluation_registry,
)
from policy_api.evaluation.tool_calling import (
    ToolCallingEvaluationInputError,
    load_tool_calling_cases,
)
from policy_api.evaluation.tool_calling_v2 import load_v2_manifest
from policy_api.models import UserRole
from policy_api.tools.definitions import ToolContext
from policy_api.tools.executor import ToolExecutor
from policy_api.tools.types import PlannedToolCall, PlannerTurn


ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "evaluation" / "hr_tool_calling_cases.json"
MANIFEST = ROOT / "evaluation" / "hr_tool_calling_case_manifest_v2.json"


class FakePlanner:
    def __init__(self, turns: list[PlannerTurn]) -> None:
        self.turns = list(turns)
        self.messages_seen: list[list[dict[str, object]]] = []
        self.tools_seen: list[list[dict[str, object]]] = []

    def complete(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        required_tool_name: str | None = None,
    ) -> PlannerTurn:
        del required_tool_name
        self.tools_seen.append(list(tools))
        self.messages_seen.append([dict(message) for message in messages])
        return self.turns.pop(0)


def call(
    call_id: str,
    provider_name: str,
    arguments: Mapping[str, object],
) -> PlannedToolCall:
    return PlannedToolCall(call_id, provider_name, dict(arguments))


def case_and_contract(case_id: str):
    cases = load_tool_calling_cases(DATASET)
    store = EvaluationFixtureStore()
    registry = build_safe_evaluation_registry(store)
    manifest = load_v2_manifest(MANIFEST, cases=cases, registry=registry)
    case = next(item for item in cases if item["id"] == case_id)
    return case, manifest[case_id]


def evaluate(case_id: str, planner: FakePlanner):
    case, contract = case_and_contract(case_id)
    return SafeRealToolFlowEvaluator(planner=planner).evaluate(case, contract)


def test_two_reads_then_write_proposal_is_one_complete_safe_flow() -> None:
    case, _contract = case_and_contract("HR-TC-007")
    planner = FakePlanner(
        [
            PlannerTurn(
                None,
                (call("r1", "hr_get_my_leave_balances", {}),),
            ),
            PlannerTurn(
                None,
                (call(
                    "r2",
                    "hr_calculate_leave_duration",
                    {"start_date": "2026-09-07", "end_date": "2026-09-09"},
                ),),
            ),
            PlannerTurn(
                None,
                (call("w1", "hr_submit_leave_request", case["expected_arguments"]),),
            ),
        ]
    )

    trace = evaluate("HR-TC-007", planner)

    assert [item.tool_name for item in trace.planned_calls] == [
        "hr.get_my_leave_balances",
        "hr.calculate_leave_duration",
        "hr.submit_leave_request",
    ]
    assert trace.terminal_kind == "write_proposal"
    assert trace.model_call_count == 3
    assert trace.read_call_count == 2
    assert trace.write_proposal_count == 1
    assert trace.write_executed is False
    assert trace.created_resource_ids == ()
    assert "UNTRUSTED_TOOL_DATA:" in str(planner.messages_seen[2])


def test_missing_fields_clarification_exposes_no_read_or_write_tools() -> None:
    planner = FakePlanner(
        [PlannerTurn("请提供请假类型、开始日期、结束日期和请假原因。", ())]
    )

    trace = evaluate("HR-TC-010", planner)

    assert trace.terminal_kind == "clarification"
    assert trace.read_call_count == 0
    assert trace.write_proposal_count == 0
    assert trace.write_executed is False


def test_terminal_cancel_profile_returns_domain_error_without_persistence() -> None:
    case, _contract = case_and_contract("HR-TC-034")
    planner = FakePlanner(
        [PlannerTurn(None, (call(
            "w1",
            "hr_cancel_leave_request",
            case["expected_arguments"],
        ),))]
    )

    trace = evaluate("HR-TC-034", planner)

    assert trace.terminal_kind == "error"
    assert trace.terminal_error == "leave_request_state_conflict"
    assert trace.write_proposal_count == 0
    assert trace.write_executed is False


def test_unsafe_scope_write_tool_is_hidden_before_domain_execution() -> None:
    case, _contract = case_and_contract("HR-TC-038")
    planner = FakePlanner(
        [
            PlannerTurn(
                None,
                (
                    call(
                        "w1",
                        "hr_cancel_leave_request",
                        case["expected_arguments"],
                    ),
                ),
            )
        ]
    )

    trace = evaluate("HR-TC-038", planner)

    assert trace.terminal_error == "tool_not_available_in_flow"
    assert trace.write_proposal_count == 0
    assert trace.write_executed is False


def test_required_duration_prevents_a_premature_text_refusal() -> None:
    planner = FakePlanner(
        [
            PlannerTurn(
                None,
                (call("r1", "hr_get_my_leave_balances", {}),),
            ),
            PlannerTurn("余额不足，无法生成请假申请。", ()),
        ]
    )

    trace = evaluate("HR-TC-007", planner)

    assert [item.tool_name for item in trace.planned_calls] == [
        "hr.get_my_leave_balances",
    ]
    assert trace.read_call_count == 1
    assert trace.terminal_kind == "error"
    assert trace.terminal_error == "tool_required_call_missing"
    assert trace.write_proposal_count == 0


def test_unknown_tool_multiple_writes_and_limits_remain_fail_closed() -> None:
    unknown = evaluate(
        "HR-TC-001",
        FakePlanner([PlannerTurn(None, (call("u1", "unknown_tool", {}),))]),
    )
    assert unknown.terminal_error == "unknown_tool"

    case, _contract = case_and_contract("HR-TC-007")
    writes = evaluate(
        "HR-TC-007",
        FakePlanner(
            [
                PlannerTurn(
                    None,
                    (
                        call("w1", "hr_submit_leave_request", case["expected_arguments"]),
                        call("w2", "hr_submit_leave_request", case["expected_arguments"]),
                    ),
                )
            ]
        ),
    )
    assert writes.terminal_error == "tool_not_available_in_flow"
    assert writes.write_proposal_count == 0

    reads = tuple(
        call(str(index), "hr_get_my_leave_balances", {"year": 2020 + index})
        for index in range(5)
    )
    read_limit = evaluate(
        "HR-TC-001",
        FakePlanner([PlannerTurn(None, reads)]),
    )
    assert read_limit.terminal_error == "tool_read_limit_exceeded"
    assert read_limit.read_call_count == 4

    converged_flow = evaluate(
        "HR-TC-001",
        FakePlanner(
            [
                PlannerTurn(None, (call(str(index), "hr_get_my_leave_balances", {}),))
                for index in range(3)
            ]
        ),
    )
    assert converged_flow.terminal_error == "tool_not_available_in_flow"
    assert converged_flow.model_call_count == 2
    assert converged_flow.read_call_count == 1


def test_evaluator_uses_hr_policy_to_converge_after_policy_evidence() -> None:
    planner = FakePlanner(
        [
            PlannerTurn(
                None,
                (call("p1", "knowledge_search_policy", {"query": "年假结转规定"}),),
            ),
            PlannerTurn("制度答复", ()),
        ]
    )

    trace = evaluate("HR-TC-006", planner)

    assert trace.terminal_kind == "text"
    assert [
        [tool["function"]["name"] for tool in tools]
        for tools in planner.tools_seen
    ] == [["knowledge_search_policy"], []]


def test_recording_planner_redacts_nested_secret_arguments() -> None:
    planner = FakePlanner(
        [
            PlannerTurn(
                None,
                (
                    call(
                        "r1",
                        "hr_get_my_leave_balances",
                        {"nested": {"api_key": "must-not-leak"}},
                    ),
                ),
            )
        ]
    )
    store = EvaluationFixtureStore()
    registry = build_safe_evaluation_registry(store)
    recording = RecordingPlanner(planner=planner, registry=registry)

    recording.complete([], tools=[])

    assert "must-not-leak" not in repr(recording.planned_calls)
    assert recording.planned_calls[0].arguments == {"nested": {}}


def test_memory_confirmation_id_is_deterministic_and_never_a_resource_id() -> None:
    case, contract = case_and_contract("HR-TC-007")
    store = EvaluationFixtureStore()
    registry = build_safe_evaluation_registry(store)
    store.activate(case=case, contract=contract)
    proposer = EvaluationWriteProposer(store)
    definition = registry.get("hr.submit_leave_request")
    context = ToolContext(
        actor_user_id=UUID("00000000-0000-0000-0000-000000000001"),
        role=UserRole.EMPLOYEE,
    )

    first = proposer(definition, case["expected_arguments"], context)
    second = proposer(definition, case["expected_arguments"], context)

    assert first.confirmation_id == second.confirmation_id
    assert first.tool_name == "hr.submit_leave_request"
    assert proposer.write_executed is False
    assert proposer.created_resource_ids == ()


def test_evaluator_rejects_multi_turn_catalog_input() -> None:
    case, contract = case_and_contract("HR-TC-001")
    case = {**case, "input_turns": [*case["input_turns"], {"role": "user", "content": "again"}]}

    with pytest.raises(ToolCallingEvaluationInputError, match="single_user_turn"):
        SafeRealToolFlowEvaluator(
            planner=FakePlanner([PlannerTurn("unused", ())])
        ).evaluate(case, contract)


def test_domain_profiles_expose_consistent_synthetic_read_facts() -> None:
    context = ToolContext(
        actor_user_id=UUID("00000000-0000-0000-0000-000000000001"),
        role=UserRole.EMPLOYEE,
    )

    balance_case, balance_contract = case_and_contract("HR-TC-022")
    store = EvaluationFixtureStore()
    registry = build_safe_evaluation_registry(store)
    store.activate(case=balance_case, contract=balance_contract)
    balance = ToolExecutor(registry).execute(
        "hr.get_my_leave_balances", {}, context
    ).result["model_result"]
    assert balance["balances"][0]["available"] == "0.0"

    happy_case, happy_contract = case_and_contract("HR-TC-008")
    store.activate(case=happy_case, contract=happy_contract)
    happy_balances = ToolExecutor(registry).execute(
        "hr.get_my_leave_balances", {"year": 2026}, context
    ).result["model_result"]
    assert happy_balances == {
        "balances": [
            {"leave_type_code": "annual", "year": 2026, "available": "10.0"},
            {"leave_type_code": "comp_time", "year": 2026, "available": "10.0"},
        ]
    }
    duration = ToolExecutor(registry).execute(
        "hr.calculate_leave_duration",
        {"start_date": "2026-09-14", "end_date": "2026-09-15"},
        context,
    ).result["model_result"]
    assert duration["workday_count"] == "2.0"

    overlap_case, overlap_contract = case_and_contract("HR-TC-025")
    store.activate(case=overlap_case, contract=overlap_contract)
    requests = ToolExecutor(registry).execute(
        "hr.list_my_leave_requests", {}, context
    ).result["model_result"]
    assert requests["requests"][0]["status"] == "pending"

    conflict_case, conflict_contract = case_and_contract("HR-TC-034")
    store.activate(case=conflict_case, contract=conflict_contract)
    request = ToolExecutor(registry).execute(
        "hr.get_my_leave_request",
        conflict_case["expected_arguments"],
        context,
    ).result["model_result"]
    assert request["status"] == "approved"

    cancelled_case, cancelled_contract = case_and_contract("HR-TC-035")
    store.activate(case=cancelled_case, contract=cancelled_contract)
    cancelled = ToolExecutor(registry).execute(
        "hr.get_my_leave_request",
        cancelled_case["expected_arguments"],
        context,
    ).result["model_result"]
    assert cancelled["status"] == "cancelled"

    rejected_case, rejected_contract = case_and_contract("HR-TC-036")
    store.activate(case=rejected_case, contract=rejected_contract)
    rejected = ToolExecutor(registry).execute(
        "hr.get_my_leave_request",
        rejected_case["expected_arguments"],
        context,
    ).result["model_result"]
    assert rejected["status"] == "rejected"
