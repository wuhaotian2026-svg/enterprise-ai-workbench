from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo

from policy_api.tools.definitions import ToolContext, ToolDefinition
from policy_api.tools.flow_policy import (
    DefaultToolFlowPolicy,
    FlowPhase,
    ToolFlowState,
)
from policy_api.tools.registry import ToolRegistry


_POLICY = "knowledge.search_policy"
_BALANCES = "hr.get_my_leave_balances"
_DURATION = "hr.calculate_leave_duration"
_LIST = "hr.list_my_leave_requests"
_GET = "hr.get_my_leave_request"
_SUBMIT = "hr.submit_leave_request"
_CANCEL = "hr.cancel_leave_request"

_UNSAFE_SCOPE_TERMS = (
    "所有人", "全部员工", "他人", "别人的", "其他人", "同事",
)
_WRITE_TERMS = ("撤销", "取消", "撤回", "提交", "申请")
_CANCEL_TERMS = ("撤销", "取消", "撤回")
_SUBMIT_TERMS = (
    "帮我申请", "我要申请", "帮我提交", "我要提交",
    "申请年假", "申请调休", "我要请假",
    "想请假", "请年假", "请调休", "再请",
)
_STATUS_TERMS = (
    "申请状态", "申请进度", "申请记录", "我的申请", "待审批",
    "当前状态", "的状态", "查看申请", "查询申请",
    "休假单", "请假申请", "申请详情",
)
_BALANCE_TERMS = (
    "余额", "剩余", "还有几天", "还剩", "可用天数", "还能休几天",
)
_DURATION_TERMS = ("几个工作日", "多少个工作日", "请假时长", "工作日数")
_POLICY_TERMS = ("制度", "规定", "结转", "资格", "权限", "政策")
_OVERLAP_TERMS = ("重叠", "已有申请", "已批准", "再请")
_TOOL_DATA_TERMS = ("工具结果", "工具内容", "工具输出")
_NEGATED_ACTION_TERMS = ("不要执行", "不得执行", "不能执行", "请忽略", "忽略它")
_SUBMIT_ACTION_PATTERN = re.compile(
    r"请(?!问|查询|查看|介绍|说明|解释|告诉)"
    r"[^，。；！？\n]{0,24}(?:年假|调休|补休)"
)
_SUBMIT_DATE_ACTION_PATTERN = re.compile(
    r"(?:^|[，。；！？\s])申请\s*(?:"
    r"\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"(?=\s|到|至|，|。)|今天|明天|后天|本周|下周|这个周|下个月)"
)
_DURATION_ACTION_PATTERN = re.compile(
    r"(?:计算|算一下|算算)[^，。；！？\n]{0,40}工作日"
)
_WRITE_ARGUMENT_NAMES = frozenset(
    {"leave_type_code", "start_date", "end_date", "reason"}
)

_LEAVE_TYPE_ALIASES = {
    "annual": "annual",
    "annual_leave": "annual",
    "年假": "annual",
    "compensatory": "compensatory",
    "comp_time": "compensatory",
    "调休": "compensatory",
    "补休": "compensatory",
}
_STATUS_ALIASES = {
    "pending": "pending",
    "待审批": "pending",
    "approved": "approved",
    "已批准": "approved",
    "rejected": "rejected",
    "已驳回": "rejected",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "已撤销": "cancelled",
}


class HrToolFlowPolicy(DefaultToolFlowPolicy):
    def __init__(
        self,
        *,
        today_provider: Callable[[], date],
        draft_fields: Mapping[str, object] | None = None,
        draft_intent: str | None = None,
    ) -> None:
        self._today_provider = today_provider
        self._draft_fields = dict(draft_fields or {})
        self._draft_intent = draft_intent

    def start(
        self,
        user_text: str,
        context: ToolContext,
        registry: ToolRegistry,
    ) -> ToolFlowState:
        intent = self._classify_intent(user_text)
        if intent == "unknown" and self._draft_intent == "submit_leave":
            intent = "submit"
        overlap_requested = any(term in user_text for term in _OVERLAP_TERMS)
        desired = self._initial_tools(intent, overlap_requested)
        authorized = self._authorized_names(context, registry)
        visible = tuple(name for name in desired if name in authorized)
        required_next_tool_name: str | None = None
        collected_argument_names: set[str] = set()
        server_supplied_argument_names: set[str] = set()
        if intent == "submit":
            server_supplied_argument_names.update(
                name for name in self._draft_fields if name in _WRITE_ARGUMENT_NAMES
            )
            collected_argument_names.update(server_supplied_argument_names)
        fact_flags: dict[str, str | bool] = {
            "overlap_requested": overlap_requested,
        }
        if intent == "unsafe_scope" and any(
            term in user_text for term in _CANCEL_TERMS
        ):
            fact_flags["policy_topic"] = (
                "leave_request_cancellation_permission"
            )
        if intent == "submit":
            missing = sorted(_WRITE_ARGUMENT_NAMES - collected_argument_names)
            if missing:
                fact_flags["missing_required_fields"] = ",".join(missing)
                visible = ()
            elif intent == "submit" and not overlap_requested:
                visible = tuple(
                    name for name in (_BALANCES,) if name in authorized
                )
                required_next_tool_name = visible[0] if visible else None
        return ToolFlowState(
            phase=(
                FlowPhase.RESPOND_ONLY
                if not visible else FlowPhase.GATHERING
            ),
            intent=intent,
            visible_tool_names=visible,
            fact_flags=fact_flags,
            collected_argument_names=collected_argument_names,
            server_supplied_argument_names=server_supplied_argument_names,
            required_next_tool_name=required_next_tool_name,
            current_date=self._today_provider(),
        )

    def normalize_arguments(
        self,
        state: ToolFlowState,
        definition: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        normalized: dict[str, object] = {}
        for key, raw_value in arguments.items():
            value: object = raw_value.strip() if isinstance(raw_value, str) else raw_value
            if key == "leave_type_code" and isinstance(value, str):
                value = _LEAVE_TYPE_ALIASES.get(value.casefold(), value)
            elif key == "status" and isinstance(value, str):
                value = _STATUS_ALIASES.get(value.casefold(), value)
            elif key == "request_id" and isinstance(value, str):
                try:
                    value = str(UUID(value))
                except ValueError:
                    pass
            normalized[key] = value
        if state.intent == "submit":
            allowed_fields = set(definition.input_model.model_fields)
            for key, value in self._draft_fields.items():
                if key in allowed_fields:
                    normalized[key] = value
        state.collected_argument_names.update(normalized)
        return normalized

    def observe_read(
        self,
        state: ToolFlowState,
        definition: ToolDefinition,
        _normalized_arguments: Mapping[str, object],
        model_result: Mapping[str, object],
    ) -> None:
        state.completed_read_names.add(definition.name)
        if definition.name == _POLICY:
            state.fact_flags["policy_result"] = str(
                model_result.get("status", "unknown")
            )
            self._respond_only(state)
            return
        if definition.name == _BALANCES:
            if self._all_balances_unavailable(model_result):
                state.fact_flags["all_balances_unavailable"] = True
                self._respond_only(state)
                return
            if state.intent == "balance":
                self._respond_only(state)
                return
            if state.intent == "submit":
                self._advance_submit(state)
            return
        if definition.name == _DURATION:
            if state.intent == "duration":
                self._respond_only(state)
                return
            if state.intent == "submit":
                self._advance_submit(state)
            return
        if definition.name == _LIST:
            if state.intent == "request_status":
                self._respond_only(state)
                return
            if state.intent == "submit":
                self._advance_submit(state)
            return
        if definition.name == _GET:
            status = str(model_result.get("status", "unknown")).casefold()
            state.fact_flags["request_status"] = status
            if state.intent == "cancel" and status == "pending":
                state.phase = FlowPhase.READY_TO_PROPOSE
                state.visible_tool_names = (_CANCEL,)
            else:
                self._respond_only(state)

    @staticmethod
    def _classify_intent(user_text: str) -> str:
        if (
            any(term in user_text for term in _UNSAFE_SCOPE_TERMS)
            and any(term in user_text for term in _WRITE_TERMS)
        ):
            return "unsafe_scope"
        if (
            any(term in user_text for term in _TOOL_DATA_TERMS)
            and any(term in user_text for term in _NEGATED_ACTION_TERMS)
        ):
            if any(term in user_text for term in _BALANCE_TERMS):
                return "balance"
            if HrToolFlowPolicy._is_duration_intent(user_text):
                return "duration"
            if any(term in user_text for term in _STATUS_TERMS):
                return "request_status"
        if any(term in user_text for term in _CANCEL_TERMS):
            return "cancel"
        if (
            any(term in user_text for term in _SUBMIT_TERMS)
            or _SUBMIT_ACTION_PATTERN.search(user_text) is not None
            or _SUBMIT_DATE_ACTION_PATTERN.search(user_text) is not None
        ):
            return "submit"
        if any(term in user_text for term in _STATUS_TERMS):
            return "request_status"
        if any(term in user_text for term in _BALANCE_TERMS):
            return "balance"
        if HrToolFlowPolicy._is_duration_intent(user_text):
            return "duration"
        if any(term in user_text for term in _POLICY_TERMS):
            return "policy"
        return "unknown"

    @staticmethod
    def _is_duration_intent(user_text: str) -> bool:
        return (
            any(term in user_text for term in _DURATION_TERMS)
            or _DURATION_ACTION_PATTERN.search(user_text) is not None
        )

    @staticmethod
    def _initial_tools(
        intent: str,
        overlap_requested: bool,
    ) -> tuple[str, ...]:
        if intent in {"policy", "unsafe_scope"}:
            return (_POLICY,)
        if intent == "balance":
            return (_BALANCES,)
        if intent == "duration":
            return (_DURATION,)
        if intent == "request_status":
            return (_LIST, _GET)
        if intent == "submit":
            reads = (_BALANCES, _DURATION)
            if overlap_requested:
                reads = (*reads, _LIST)
            return (*reads, _SUBMIT)
        if intent == "cancel":
            return (_GET, _CANCEL)
        return ()

    @staticmethod
    def _authorized_names(
        context: ToolContext,
        registry: ToolRegistry,
    ) -> set[str]:
        authorized: set[str] = set()
        for tool in registry.provider_tools(role=context.role):
            function = tool.get("function")
            if not isinstance(function, dict):
                continue
            provider_name = function.get("name")
            if isinstance(provider_name, str):
                authorized.add(registry.resolve_provider_name(provider_name).name)
        return authorized

    @staticmethod
    def _all_balances_unavailable(model_result: Mapping[str, object]) -> bool:
        balances = model_result.get("balances")
        if not isinstance(balances, list) or not balances:
            return False
        available_values: list[Decimal] = []
        for item in balances:
            if not isinstance(item, Mapping):
                return False
            try:
                available_values.append(Decimal(str(item["available"])))
            except (KeyError, InvalidOperation, ValueError):
                return False
        return bool(available_values) and all(
            value <= 0 for value in available_values
        )

    @staticmethod
    def _respond_only(state: ToolFlowState) -> None:
        state.phase = FlowPhase.RESPOND_ONLY
        state.visible_tool_names = ()
        state.required_next_tool_name = None

    @staticmethod
    def _advance_submit(state: ToolFlowState) -> None:
        remaining: list[str] = []
        if _BALANCES not in state.completed_read_names:
            remaining.append(_BALANCES)
        if _DURATION not in state.completed_read_names:
            remaining.append(_DURATION)
        if (
            state.fact_flags.get("overlap_requested") is True
            and _LIST not in state.completed_read_names
        ):
            remaining.append(_LIST)
        if state.fact_flags.get("overlap_requested") is True:
            if not remaining:
                state.phase = FlowPhase.READY_TO_PROPOSE
            state.visible_tool_names = (*remaining, _SUBMIT)
            state.required_next_tool_name = None
            return
        if remaining:
            state.visible_tool_names = (remaining[0],)
            state.required_next_tool_name = remaining[0]
            return
        state.phase = FlowPhase.READY_TO_PROPOSE
        state.visible_tool_names = (_SUBMIT,)
        state.required_next_tool_name = _SUBMIT


def build_hr_tool_flow_policy(
    *,
    today_provider: Callable[[], date] | None = None,
    draft_fields: Mapping[str, object] | None = None,
    draft_intent: str | None = None,
) -> HrToolFlowPolicy:
    provider = today_provider or (
        lambda: datetime.now(ZoneInfo("Asia/Shanghai")).date()
    )
    return HrToolFlowPolicy(
        today_provider=provider,
        draft_fields=draft_fields,
        draft_intent=draft_intent,
    )
