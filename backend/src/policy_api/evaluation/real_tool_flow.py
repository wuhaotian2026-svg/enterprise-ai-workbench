from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from time import perf_counter
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from policy_api.evaluation.tool_calling import ToolCallingEvaluationInputError
from policy_api.evaluation.tool_calling_v2 import (
    CLARIFICATION_ALIASES,
    V2CaseContract,
    V2FlowTrace,
    V2PlannedCall,
)
from policy_api.hr.tools import build_hr_tool_definitions
from policy_api.hr.tool_flow_policy import build_hr_tool_flow_policy
from policy_api.knowledge.tools import build_knowledge_tool_definition
from policy_api.models import UserRole
from policy_api.tools.authorization import authorize_tool
from policy_api.tools.definitions import (
    ToolContext,
    ToolDefinition,
)
from policy_api.tools.errors import ToolError
from policy_api.tools.executor import ToolExecutor
from policy_api.tools.orchestrator import (
    BoundedToolOrchestrator,
    Planner,
    SYSTEM_MESSAGE,
)
from policy_api.tools.registry import ToolRegistry
from policy_api.tools.schemas import ConfirmationBlock, ErrorBlock, TextBlock
from policy_api.tools.types import PlannerTurn


REFERENCE_DATE = "2026-08-16"
ORCHESTRATOR_LIMITS = {"model": 3, "read": 4, "write": 1}
_FIXED_NOW = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
_SECRET_KEY_PARTS = ("api_key", "authorization", "password", "secret", "token")
_REFUSAL_TERMS = ("不能", "无法", "不可以", "拒绝", "无权", "不会")


def evaluation_system_message(reference_date: str = REFERENCE_DATE) -> str:
    return (
        f"{SYSTEM_MESSAGE} The evaluation reference date is {reference_date}. "
        "Use that date when resolving relative dates."
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SECRET_KEY_PARTS)
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class _ReferenceDatePlanner:
    def __init__(self, planner: Planner, reference_date: str) -> None:
        self._planner = planner
        self._system_message = evaluation_system_message(reference_date)

    def complete(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        required_tool_name: str | None = None,
    ) -> PlannerTurn:
        enriched = [dict(message) for message in messages]
        if enriched and enriched[0].get("role") == "system":
            enriched[0]["content"] = self._system_message
        return self._planner.complete(
            enriched,
            tools=tools,
            required_tool_name=required_tool_name,
        )


class RecordingPlanner:
    def __init__(self, *, planner: Planner, registry: ToolRegistry) -> None:
        self._planner = planner
        self._registry = registry
        self.planned_calls: list[V2PlannedCall] = []
        self.latencies_ms: list[int] = []

    def complete(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        required_tool_name: str | None = None,
    ) -> PlannerTurn:
        started = perf_counter()
        try:
            turn = self._planner.complete(
                messages,
                tools=tools,
                required_tool_name=required_tool_name,
            )
        finally:
            self.latencies_ms.append(max(0, round((perf_counter() - started) * 1000)))
        for tool_call in turn.tool_calls:
            try:
                tool_name = self._registry.resolve_provider_name(tool_call.name).name
            except ToolError:
                tool_name = tool_call.name
            arguments = _redact(tool_call.arguments)
            self.planned_calls.append(
                V2PlannedCall(
                    tool_name=tool_name,
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        return turn


class EvaluationFixtureStore:
    def __init__(self) -> None:
        self.case: dict[str, Any] | None = None
        self.contract: V2CaseContract | None = None

    def activate(
        self,
        *,
        case: dict[str, Any],
        contract: V2CaseContract,
    ) -> None:
        self.case = case
        self.contract = contract

    def handler(
        self,
        tool_name: str,
    ):
        def fixture_handler(
            _context: ToolContext,
            input_data: BaseModel,
        ) -> Mapping[str, object]:
            if self.case is None or self.contract is None:
                raise ToolError("evaluation_fixture_not_active")
            if (
                self.contract.expected_terminal == "error"
                and self.contract.expected_error
                and self.contract.target_tool == tool_name
            ):
                raise ToolError(self.contract.expected_error)
            normalized = input_data.model_dump(mode="json", exclude_none=True)
            if tool_name == "hr.get_my_leave_balances":
                available = (
                    "0.0"
                    if self.contract.profile == "insufficient_balance"
                    else "10.0"
                )
                model_result: object = {
                    "balances": [
                        {
                            "leave_type_code": "annual",
                            "year": normalized.get("year", 2026),
                            "available": available,
                        },
                        {
                            "leave_type_code": "comp_time",
                            "year": normalized.get("year", 2026),
                            "available": available,
                        },
                    ]
                }
            elif tool_name == "hr.calculate_leave_duration":
                start = date.fromisoformat(str(normalized["start_date"]))
                end = date.fromisoformat(str(normalized["end_date"]))
                cursor = start
                workdays = 0
                while cursor <= end:
                    workdays += cursor.weekday() < 5
                    cursor += timedelta(days=1)
                model_result = {
                    **normalized,
                    "workday_count": f"{workdays}.0",
                }
            elif tool_name == "hr.list_my_leave_requests":
                if self.contract.profile == "overlap":
                    expected = self.case.get("expected_arguments", {})
                    model_result = {
                        "requests": [
                            {
                                "request_id": "00000000-0000-0000-0000-000000000025",
                                "status": "pending",
                                "start_date": expected.get("start_date"),
                                "end_date": expected.get("end_date"),
                            }
                        ]
                    }
                else:
                    model_result = {"requests": []}
            elif tool_name == "hr.get_my_leave_request":
                status_by_case = {
                    "HR-TC-034": "approved",
                    "HR-TC-035": "cancelled",
                    "HR-TC-036": "rejected",
                }
                model_result = {
                    "request_id": normalized.get("request_id"),
                    "status": status_by_case.get(str(self.case["id"]), "pending"),
                }
            elif tool_name == "knowledge.search_policy":
                model_result = {
                    "status": "sufficient",
                    "text": "Synthetic enterprise leave policy evidence.",
                    "citations": [],
                }
            else:
                raise ToolError("unknown_tool")
            return {
                "blocks": [
                    {
                        "type": "business_facts",
                        "facts": [
                            {
                                "label": tool_name,
                                "value": model_result,
                            }
                        ],
                        "queried_at": _FIXED_NOW.isoformat(),
                    }
                ],
                "model_result": model_result,
            }

        return fixture_handler


def _unused_policy_search(_query: str):
    raise RuntimeError("evaluation_original_handler_forbidden")


def build_safe_evaluation_registry(store: EvaluationFixtureStore) -> ToolRegistry:
    source_definitions = (
        build_knowledge_tool_definition(_unused_policy_search),
        *build_hr_tool_definitions(cast(Session, None)),
    )
    definitions = tuple(
        replace(definition, handler=store.handler(definition.name))
        for definition in source_definitions
    )
    return ToolRegistry(definitions)


class EvaluationWriteProposer:
    def __init__(self, store: EvaluationFixtureStore) -> None:
        self._store = store
        self.proposed_calls: list[V2PlannedCall] = []
        self.write_executed = False
        self.created_resource_ids: tuple[str, ...] = ()

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ConfirmationBlock:
        case = self._store.case
        contract = self._store.contract
        if case is None or contract is None:
            raise ToolError("evaluation_fixture_not_active")
        authorize_tool(definition, context)
        if set(arguments) - set(definition.input_model.model_fields):
            raise ToolError("invalid_tool_arguments")
        try:
            parsed = definition.input_model.model_validate(dict(arguments))
        except ValidationError:
            raise ToolError("invalid_tool_arguments") from None
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        self.proposed_calls.append(V2PlannedCall(definition.name, normalized))
        if (
            contract.expected_terminal == "error"
            and contract.expected_error
            and contract.target_tool == definition.name
        ):
            raise ToolError(contract.expected_error)
        confirmation_id = uuid5(
            NAMESPACE_URL,
            f"hr-real-evaluation:{case['id']}:{definition.name}",
        )
        return ConfirmationBlock(
            confirmation_id=confirmation_id,
            tool_name=definition.name,
            preview=normalized,
            expires_at=_FIXED_NOW + timedelta(minutes=15),
        )


class SafeRealToolFlowEvaluator:
    def __init__(
        self,
        *,
        planner: Planner,
        reference_date: str = REFERENCE_DATE,
    ) -> None:
        self.store = EvaluationFixtureStore()
        self.registry = build_safe_evaluation_registry(self.store)
        self.recording_planner = RecordingPlanner(
            planner=planner,
            registry=self.registry,
        )
        self.proposer = EvaluationWriteProposer(self.store)
        self._reference_date = reference_date
        self.orchestrator = self._build_orchestrator(draft_fields={})
        self.context = ToolContext(
            actor_user_id=UUID("00000000-0000-0000-0000-000000000001"),
            role=UserRole.EMPLOYEE,
        )

    def _build_orchestrator(
        self,
        *,
        draft_fields: Mapping[str, object],
    ) -> BoundedToolOrchestrator:
        return BoundedToolOrchestrator(
            planner=self.recording_planner,
            registry=self.registry,
            executor=ToolExecutor(self.registry),
            propose_write=self.proposer,
            max_model_calls=ORCHESTRATOR_LIMITS["model"],
            max_read_calls=ORCHESTRATOR_LIMITS["read"],
            max_write_proposals=ORCHESTRATOR_LIMITS["write"],
            flow_policy=build_hr_tool_flow_policy(
                today_provider=lambda: date.fromisoformat(self._reference_date),
                draft_fields=draft_fields,
            ),
        )

    def evaluate(
        self,
        case: dict[str, Any],
        contract: V2CaseContract,
    ) -> V2FlowTrace:
        turns = case.get("input_turns")
        if (
            not isinstance(turns, list)
            or len(turns) != 1
            or not isinstance(turns[0], dict)
            or turns[0].get("role") != "user"
            or not isinstance(turns[0].get("content"), str)
        ):
            raise ToolCallingEvaluationInputError("invalid_v2_single_user_turn")
        raw_draft_fields = case.get(
            "validated_draft_fields",
            case.get("expected_arguments", {}),
        )
        if not isinstance(raw_draft_fields, Mapping) or any(
            not isinstance(name, str) for name in raw_draft_fields
        ):
            raise ToolCallingEvaluationInputError(
                "invalid_v2_validated_draft_fields"
            )
        self.orchestrator = self._build_orchestrator(
            draft_fields=dict(raw_draft_fields)
        )
        self.store.activate(case=case, contract=contract)
        planned_start = len(self.recording_planner.planned_calls)
        started = perf_counter()
        response = self.orchestrator.run(turns[0]["content"], self.context)
        latency_ms = max(0, round((perf_counter() - started) * 1000))

        final_text = next(
            (
                block.text
                for block in reversed(response.blocks)
                if isinstance(block, TextBlock)
            ),
            None,
        )
        error = next(
            (
                block.code
                for block in reversed(response.blocks)
                if isinstance(block, ErrorBlock)
            ),
            None,
        )
        if error is not None:
            terminal_kind = "error"
        elif any(block.type == "confirmation" for block in response.blocks):
            terminal_kind = "write_proposal"
        elif final_text and any(
            alias in final_text
            for aliases in CLARIFICATION_ALIASES.values()
            for alias in aliases
        ):
            terminal_kind = "clarification"
        elif final_text and any(term in final_text for term in _REFUSAL_TERMS):
            terminal_kind = "safe_refusal"
        else:
            terminal_kind = "text"

        return V2FlowTrace(
            planned_calls=tuple(
                self.recording_planner.planned_calls[planned_start:]
            ),
            final_text=final_text,
            terminal_kind=terminal_kind,
            terminal_error=error,
            model_call_count=response.model_calls,
            read_call_count=response.read_calls,
            write_proposal_count=response.write_proposals,
            latency_ms=latency_ms,
            write_executed=self.proposer.write_executed,
            created_resource_ids=self.proposer.created_resource_ids,
        )
