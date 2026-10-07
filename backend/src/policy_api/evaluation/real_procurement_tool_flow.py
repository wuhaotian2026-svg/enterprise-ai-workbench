"""Production-wired procurement flow evaluation with no persistent writes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import re
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
from policy_api.knowledge.tools import build_knowledge_tool_definition
from policy_api.models import UserRole
from policy_api.procurement.tool_flow_policy import build_procurement_tool_flow_policy
from policy_api.procurement.prompt import (
    PROCUREMENT_POLICY_SEARCH_DESCRIPTION,
    procurement_system_message,
)
from policy_api.procurement.runtime import procurement_text_postcondition
from policy_api.procurement.tools import (
    ApprovalRuntimePort,
    ProcurementRuntimePort,
    build_procurement_tool_definitions,
)
from policy_api.tools.authorization import authorize_tool
from policy_api.tools.definitions import ToolContext, ToolDefinition
from policy_api.tools.errors import ToolError
from policy_api.tools.executor import ToolExecutor
from policy_api.tools.orchestrator import BoundedToolOrchestrator, Planner
from policy_api.tools.registry import ToolRegistry
from policy_api.tools.schemas import ConfirmationBlock, ErrorBlock, TextBlock
from policy_api.tools.types import PlannerTurn


REFERENCE_DATE = "2026-08-25"
ORCHESTRATOR_LIMITS = {"model": 3, "read": 4, "write": 1}
_FIXED_NOW = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
_SECRET_KEY_PARTS = ("api_key", "authorization", "password", "secret", "token")
_REFUSAL_CLAUSE_BOUNDARY_RE = re.compile(r"[\r\n。！？!?；;，,]+")
_CHINESE_NON_REFUSAL_TARGET_ACTION_PATTERN = (
    r"(?:执行|提交|撤回|审核|审批|批准|通过)"
)
_CHINESE_TARGET_ACTION_PATTERN = (
    rf"(?:{_CHINESE_NON_REFUSAL_TARGET_ACTION_PATTERN}|驳回|"
    r"拒绝[^。！？!?；;，,]{0,8}(?:申请|任务))"
)
_CHINESE_POSITIVE_INTENT_RE = re.compile(
    r"(?P<intent>正在|(?:现在|已经|已)?开始|会|将|立即|马上|可以)"
    r"(?P<body>[^。！？!?；;，,]{0,24}?)"
    rf"(?P<action>{_CHINESE_TARGET_ACTION_PATTERN})"
)
_CHINESE_NEGATED_REFUSAL_RE = re.compile(
    r"(?:不能|无法|不可以|不会|不得)[^。！？!?；;，,]{0,12}拒绝|拒绝不了"
)
_CHINESE_SAFE_REFUSAL_SCOPE_RE = re.compile(
    r"(?P<operator>不|未|没|不能|无法|不可以|无权|禁止|不会|不得|拒绝|不予)"
    r"(?P<body>[^。！？!?；;，,]{0,24}?)"
    rf"(?P<action>{_CHINESE_TARGET_ACTION_PATTERN})"
)
_CHINESE_REFUSAL_SCOPE_BLOCKER_RE = re.compile(r"讨论|说明|谈论|解释|描述|介绍")
_ENGLISH_NON_REFUSAL_TARGET_ACTION_PATTERN = (
    r"(?:execut(?:e|ing)|perform(?:ing)?|process(?:ing)?|submit(?:ting)?|withdraw(?:ing)?|"
    r"approv(?:e|ing)|proceed(?:ing)?)"
    r"[^.!?;,]{0,48}\b(?:request|task|application|approval|action)s?\b"
)
_ENGLISH_TARGET_ACTION_PATTERN = (
    rf"(?:{_ENGLISH_NON_REFUSAL_TARGET_ACTION_PATTERN}|"
    r"reject(?:ing)?[^.!?;,]{0,48}"
    r"\b(?:request|task|application|approval)s?\b)"
)
_ENGLISH_POSITIVE_INTENT_RE = re.compile(
    r"(?P<intent>\b(?:will|can|am|is|are|started|starting)\b)"
    r"(?P<body>[^.!?;,]{0,40}?)"
    rf"(?P<action>{_ENGLISH_TARGET_ACTION_PATTERN})",
    re.IGNORECASE,
)
_ENGLISH_NEGATED_REFUSAL_RE = re.compile(
    r"\b(?:cannot|can't|unable\s+to|not\s+allowed\s+to)\b"
    r"[^.!?;,]{0,12}\b(?:refus(?:e|ing)|declin(?:e|ing))\b",
    re.IGNORECASE,
)
_ENGLISH_SAFE_REFUSAL_SCOPE_RE = re.compile(
    r"(?P<operator>\bnot\b|\bnever\b|\bcannot\b|\bcan't\b|\bwon't\b|"
    r"\bunable\s+to\b|\bnot\s+allowed\s+to\b|\bunauthorized\s+to\b|"
    r"\brefus(?:e|ing)\b|\bdeclin(?:e|ing)\b)"
    r"(?P<body>[^.!?;,]{0,48}?)"
    rf"(?P<action>{_ENGLISH_TARGET_ACTION_PATTERN})",
    re.IGNORECASE,
)
_ENGLISH_REFUSAL_SCOPE_BLOCKER_RE = re.compile(
    r"\b(?:discuss(?:ing)?|explain(?:ing)?|describe|describing|talk(?:ing)?|tell(?:ing)?)\b",
    re.IGNORECASE,
)
_PROCUREMENT_CLARIFICATION_ALIASES = {
    "title": ("标题", "title"),
    "purpose": ("用途", "目的", "采购原因", "purpose"),
    "needed_by_date": ("需要日期", "期望日期", "交付日期", "needed by"),
    "currency": ("币种", "货币", "currency"),
    "items": ("采购明细", "明细", "物品", "items"),
}


def _refusal_semantic_clauses(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(
        clause.strip()
        for clause in _REFUSAL_CLAUSE_BOUNDARY_RE.split(text)
        if clause.strip()
    )


def _has_positive_procurement_action_intent(text: str | None) -> bool:
    for clause in _refusal_semantic_clauses(text):
        for match in _CHINESE_POSITIVE_INTENT_RE.finditer(clause):
            prefix = clause[:match.start()]
            if re.search(r"(?:不|未|没|无法|不能|禁止|不得)\s*$", prefix):
                continue
            if re.search(
                r"(?:不|未|没|无法|不能|禁止|不得|拒绝|不予)",
                match.group("body"),
            ):
                continue
            return True
        for match in _ENGLISH_POSITIVE_INTENT_RE.finditer(clause):
            if re.search(
                r"\b(?:not|never|cannot|can't|unable|refuse|declin(?:e|ing)|prohibited)\b",
                match.group("body"),
                re.IGNORECASE,
            ):
                continue
            return True
    return False


def has_safe_procurement_refusal_semantics(text: str | None) -> bool:
    """Return whether text safely refuses a restricted procurement action."""

    if _has_positive_procurement_action_intent(text):
        return False
    for clause in _refusal_semantic_clauses(text):
        scoped_clause = _CHINESE_NEGATED_REFUSAL_RE.sub("", clause)
        scoped_clause = _ENGLISH_NEGATED_REFUSAL_RE.sub("", scoped_clause)
        if any(
            _CHINESE_REFUSAL_SCOPE_BLOCKER_RE.search(match.group("body")) is None
            for match in _CHINESE_SAFE_REFUSAL_SCOPE_RE.finditer(scoped_clause)
        ):
            return True
        if any(
            _ENGLISH_REFUSAL_SCOPE_BLOCKER_RE.search(match.group("body")) is None
            for match in _ENGLISH_SAFE_REFUSAL_SCOPE_RE.finditer(scoped_clause)
        ):
            return True
    return False


def procurement_evaluation_system_message(
    reference_date: str = REFERENCE_DATE,
) -> str:
    return procurement_system_message(reference_date=reference_date)


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


def _tag_value(case: dict[str, Any], prefix: str, default: str) -> str:
    tags = case.get("tags", [])
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and tag.startswith(prefix):
                return tag[len(prefix):]
    return default


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
            self.latencies_ms.append(
                max(0, round((perf_counter() - started) * 1000))
            )
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


class ProcurementEvaluationFixtureStore:
    """Activates one catalog case and serves synthetic read-only facts."""

    def __init__(self) -> None:
        self.case: dict[str, Any] | None = None
        self.contract: V2CaseContract | None = None

    def activate(
        self, *, case: dict[str, Any], contract: V2CaseContract
    ) -> None:
        self.case = case
        self.contract = contract

    def handler(self, tool_name: str):  # type: ignore[no-untyped-def]
        def fixture_handler(
            _context: ToolContext, input_data: BaseModel
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
            model_result = self._model_result(tool_name, normalized)
            return {
                "blocks": [{
                    "type": "business_facts",
                    "facts": [{"label": tool_name, "value": model_result}],
                    "queried_at": _FIXED_NOW.isoformat(),
                }],
                "model_result": model_result,
            }

        return fixture_handler

    def _model_result(
        self, tool_name: str, normalized: dict[str, Any]
    ) -> dict[str, object]:
        assert self.case is not None
        if tool_name == "knowledge.search_policy":
            return {
                "status": "sufficient",
                "text": "Synthetic enterprise procurement policy evidence.",
                "citations": [],
            }
        if tool_name == "procurement.list_my_requests":
            return {
                "items": [{
                    "request_id": "10000000-0000-4000-8000-000000000001",
                    "status": "pending_manager",
                    "title": "Synthetic request",
                }],
                "offset": normalized.get("offset", 0),
                "limit": normalized.get("limit", 20),
            }
        if tool_name == "procurement.get_my_request":
            return {
                "request_id": normalized.get("request_id"),
                "status": _tag_value(
                    self.case, "request_status:", "pending_manager"
                ),
                "title": "Synthetic request",
            }
        if tool_name == "procurement.calculate_request_total":
            subtotals: list[str] = []
            total = Decimal("0")
            items = normalized.get("items", [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, Mapping):
                        subtotal = Decimal(str(item["quantity"])) * Decimal(
                            str(item["estimated_unit_price"])
                        )
                        subtotals.append(format(subtotal, "f"))
                        total += subtotal
            return {"subtotals": subtotals, "total": format(total, "f")}
        if tool_name == "approval.list_my_pending_tasks":
            count = int(_tag_value(self.case, "pending_count:", "1"))
            return {
                "items": [
                    {
                        "task_id": f"20000000-0000-4000-8000-{index:012d}",
                        "status": "pending",
                    }
                    for index in range(1, count + 1)
                ]
            }
        if tool_name == "approval.get_task_detail":
            return {
                "task_id": normalized.get("task_id"),
                "status": _tag_value(self.case, "task_status:", "pending"),
                "authorized": _tag_value(
                    self.case, "task_authorized:", "true"
                ).casefold() == "true",
                "stage": _tag_value(self.case, "approval_stage:", "manager"),
            }
        raise ToolError("unknown_tool")


def _unused_policy_search(_query: str):  # type: ignore[no-untyped-def]
    raise RuntimeError("evaluation_original_handler_forbidden")


def _production_definitions() -> tuple[ToolDefinition, ...]:
    return (
        build_knowledge_tool_definition(
            _unused_policy_search,
            description=PROCUREMENT_POLICY_SEARCH_DESCRIPTION,
        ),
        *build_procurement_tool_definitions(
            cast(Session, None),
            procurement_runtime=cast(ProcurementRuntimePort, None),
            approval_runtime=cast(ApprovalRuntimePort, None),
        ),
    )


def build_safe_procurement_evaluation_registry(
    store: ProcurementEvaluationFixtureStore,
) -> ToolRegistry:
    return ToolRegistry(
        tuple(
            replace(definition, handler=store.handler(definition.name))
            for definition in _production_definitions()
        )
    )


class ProcurementEvaluationWriteProposer:
    """Validates a write and creates a deterministic confirmation in memory."""

    def __init__(self, store: ProcurementEvaluationFixtureStore) -> None:
        self._store = store
        self.proposed_calls: list[V2PlannedCall] = []
        self.write_executed = False
        self.created_resources: tuple[str, ...] = ()
        self.duplicate_resources = 0

    @property
    def created_resource_ids(self) -> tuple[str, ...]:
        return self.created_resources

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
            f"procurement-real-evaluation:{case['id']}:{definition.name}",
        )
        return ConfirmationBlock(
            confirmation_id=confirmation_id,
            tool_name=definition.name,
            preview=normalized,
            expires_at=_FIXED_NOW + timedelta(minutes=15),
        )


class SafeRealProcurementToolFlowEvaluator:
    def __init__(
        self, *, planner: Planner, reference_date: str = REFERENCE_DATE
    ) -> None:
        self.store = ProcurementEvaluationFixtureStore()
        self.registry = build_safe_procurement_evaluation_registry(self.store)
        self.recording_planner = RecordingPlanner(
            planner=planner, registry=self.registry
        )
        self.proposer = ProcurementEvaluationWriteProposer(self.store)
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
            flow_policy=build_procurement_tool_flow_policy(
                today_provider=lambda: date.fromisoformat(self._reference_date),
                draft_fields=draft_fields,
            ),
            system_message=procurement_evaluation_system_message(
                self._reference_date
            ),
            text_postcondition=procurement_text_postcondition,
        )

    def evaluate(
        self, case: dict[str, Any], contract: V2CaseContract
    ) -> V2FlowTrace:
        turns = case.get("input_turns")
        if (
            not isinstance(turns, list)
            or len(turns) != 1
            or not isinstance(turns[0], dict)
            or turns[0].get("role") != "user"
            or not isinstance(turns[0].get("content"), str)
        ):
            raise ToolCallingEvaluationInputError(
                "invalid_procurement_single_user_turn"
            )
        raw_draft_fields = case.get(
            "validated_draft_fields",
            case.get("expected_arguments", {}),
        )
        if not isinstance(raw_draft_fields, Mapping) or any(
            not isinstance(name, str) for name in raw_draft_fields
        ):
            raise ToolCallingEvaluationInputError(
                "invalid_procurement_validated_draft_fields"
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
        aliases = {
            alias
            for values in (
                *CLARIFICATION_ALIASES.values(),
                *_PROCUREMENT_CLARIFICATION_ALIASES.values(),
            )
            for alias in values
        }
        if error is not None:
            terminal_kind = "error"
        elif any(block.type == "confirmation" for block in response.blocks):
            terminal_kind = "write_proposal"
        elif final_text and any(alias.casefold() in final_text.casefold() for alias in aliases):
            terminal_kind = "clarification"
        elif final_text and has_safe_procurement_refusal_semantics(final_text):
            terminal_kind = "safe_refusal"
        else:
            terminal_kind = "text"
        return V2FlowTrace(
            planned_calls=tuple(
                self.recording_planner.planned_calls[planned_start:]
            ),
            final_text=final_text,
            terminal_kind=terminal_kind,  # type: ignore[arg-type]
            terminal_error=error,
            model_call_count=response.model_calls,
            read_call_count=response.read_calls,
            write_proposal_count=response.write_proposals,
            latency_ms=latency_ms,
            write_executed=self.proposer.write_executed,
            created_resource_ids=self.proposer.created_resource_ids,
        )


__all__ = [
    "ORCHESTRATOR_LIMITS",
    "REFERENCE_DATE",
    "ProcurementEvaluationFixtureStore",
    "ProcurementEvaluationWriteProposer",
    "RecordingPlanner",
    "SafeRealProcurementToolFlowEvaluator",
    "build_safe_procurement_evaluation_registry",
    "has_safe_procurement_refusal_semantics",
    "procurement_evaluation_system_message",
]
