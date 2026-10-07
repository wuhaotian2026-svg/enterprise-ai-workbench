from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from uuid import UUID

from policy_api.tools.definitions import ToolContext, ToolDefinition
from policy_api.tools.flow_policy import (
    DefaultToolFlowPolicy,
    FlowPhase,
    ToolFlowState,
)
from policy_api.tools.registry import ToolRegistry


PROCUREMENT_INTENTS = {
    "policy", "calculate", "list_requests", "request_detail",
    "draft_request", "submit_request", "withdraw_request", "list_tasks", "task_detail",
    "approve_task", "reject_task", "unknown",
}

_POLICY = "knowledge.search_policy"
_LIST_REQUESTS = "procurement.list_my_requests"
_GET_REQUEST = "procurement.get_my_request"
_CALCULATE = "procurement.calculate_request_total"
_SUBMIT = "procurement.submit_request"
_WITHDRAW = "procurement.withdraw_request"
_LIST_TASKS = "approval.list_my_pending_tasks"
_GET_TASK = "approval.get_task_detail"
_APPROVE = "approval.approve_task"
_REJECT = "approval.reject_task"

_SUBMIT_FIELDS = frozenset({
    "title", "purpose", "needed_by_date", "currency", "items",
})
_VALIDATED_ARGUMENT_FIELDS = frozenset({
    *_SUBMIT_FIELDS,
    "request_id",
    "task_id",
    "reason",
    "comment",
})
_UUID_PATTERN = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}(?![0-9a-fA-F])"
)
_POLICY_TERMS = ("制度", "规定", "政策", "权限", "规则", "能否", "是否允许")
_POLICY_PERMISSION_QUERY_PATTERN = re.compile(r"(?:能否|是否允许|可否|吗|么|[？?])")
_POLICY_PERMISSION_SCOPE_PATTERN = re.compile(
    r"(?:代办|代为|批量|全部|所有|他人|别人|其他人|其他员工|其他同事|"
    r"另一(?:个|位)?人|非本人|冒充|假冒|假扮|伪装成|忽略|无视|绕过|跳过|规避)"
)
_POLICY_PERMISSION_ACTION_PATTERN = re.compile(
    r"(?:审批|批准|通过|同意|拒绝|驳回|撤回|撤销|提交|创建|发起)"
)
_CANONICAL_POLICY_QUERY_BY_FAMILY = {
    "amount_review": "采购申请金额复审规则",
    "withdraw_status": "采购申请撤回允许状态",
    "delegated_bulk_permission": "采购审批代办与批量操作权限",
}
_WRITE_ACTION_PATTERN = re.compile(
    r"(?:提交|发起|创建|撤回|撤销|批准|同意|通过|拒绝|驳回)"
    r"|\b(?:submit|create|withdraw|approve|reject)\b",
    re.IGNORECASE,
)
_RESTRICTED_WRITE_CONTEXT_PATTERN = re.compile(
    r"(?:批量|全部|所有|每(?:个|一项|一份))"
    r"[^，。；！？\n]{0,16}(?:任务|申请|采购单)"
    r"|(?:任务|申请|采购单)[^，。；！？\n]{0,16}(?:批量|全部|所有)"
    r"|(?:他人|别人的|其他人的?|另一(?:个|位)人的?|非本人的?)"
    r"[^，。；！？\n]{0,16}(?:任务|申请|采购单)"
    r"|(?:冒充|假冒|假扮|伪装成)[^，。；！？\n]{0,12}"
    r"(?:管理员|审批人|负责人|角色)"
    r"|(?:以|用|使用)[^，。；！？\n]{0,8}(?:管理员|审批人|负责人)"
    r"[^，。；！？\n]{0,4}(?:身份|权限)"
    r"|(?:忽略|无视|绕过|跳过|规避)[^，。；！？\n]{0,16}"
    r"(?:规则|制度|政策|权限|确认|审批|校验)"
    r"|\b(?:bulk|all|every)\b.{0,24}\b(?:tasks?|requests?)\b"
    r"|\b(?:another|other)\s+(?:user|person|people)(?:'s|s')?\b"
    r".{0,24}\b(?:tasks?|requests?)\b"
    r"|\b(?:impersonat(?:e|ing)|pretend(?:ing)?\s+to\s+be)\b"
    r"|\b(?:ignore|bypass|skip)\b.{0,24}"
    r"\b(?:rules?|polic(?:y|ies)|confirmations?|checks?|approval)\b",
    re.IGNORECASE,
)
_OTHER_OWNER_CONTEXT_PATTERN = re.compile(
    r"(?:他人|别人|其他人|其他用户|另一(?:个|位)(?:人|用户)|非本人)"
    r"(?:的|所属)"
    r"|(?:属于|归属于|归属(?:于)?|所有人(?:是|为)?)"
    r"[^，。；！？\n]{0,8}"
    r"(?:他人|别人|其他人|其他用户|另一(?:个|位)(?:人|用户)|非本人)"
    r"|\b(?:belongs?\s+to|owned\s+by)\s+(?:another|other)\s+"
    r"(?:user|person)\b"
    r"|\b(?:another|other)\s+(?:user|person)(?:'s|s')\b",
    re.IGNORECASE,
)
_UNSAFE_WRITE_CONTEXT_PATTERN = re.compile(
    r"(?:不|没|未|无意|犹豫|考虑|反对|禁止|暂缓|待定|"
    r"是否|能否|可否|要不要|该不该|值不值得|建议|应该|应当|"
    r"取消|别|请勿|无需|不必|不用|并非|不是|判断|怎么看|吗|么|[？?])"
)
_EXPLICIT_WRITE_ACTION_NEGATION_PATTERN = re.compile(
    r"(?:(?:(?:建议|请)\s*)?(?:不要|别)|"
    r"不想|不打算|没有打算|没打算|不愿|不能|不可|不再|不需要|无需|"
    r"不必|不用|没有必要|没必要|请勿|并非(?:要)?|不是(?:要)?|"
    r"无意|反对|禁止|暂缓|取消|没|未|不)\s*"
    r"(?:再(?:次)?)?\s*"
    r"(?:提交|发起|创建|撤回|撤销|批准|同意|通过|拒绝|驳回)"
)
_DIRECT_TAIL_ACTION_UNCERTAINTY_PATTERN = re.compile(
    r"(?:犹豫|考虑|待定|是否|能否|可否|要不要|该不该|值不值得|"
    r"建议|应该|应当|判断|怎么看)"
    r"[^，。；！？\n]{0,24}"
    r"(?:提交|发起|创建|撤回|撤销|批准|同意|通过|拒绝|驳回)"
)
_DIRECT_TAIL_FIRST_PERSON_HESITATION_PATTERN = re.compile(
    r"(?:我|我们|本人)[^，。；！？\n]{0,8}"
    r"(?:不想|不愿|不打算|没想好|尚未决定|未决定)"
)
_DIRECT_TAIL_DEICTIC_NEGATION_PATTERN = re.compile(
    r"(?:(?:建议|提议|请)?(?:不要|不应|别)|不想|不愿|不打算)"
    r"[^，。；！？\n]{0,8}"
    r"(?:这样做|这么做|这样处理|这么处理|该操作|这个操作|此操作)"
)
_UNSAFE_WRITE_QUESTION_PATTERN = re.compile(r"(?:吗|么|[？?])")
_INFORMATION_QUESTION_MARKER_PATTERN = re.compile(
    r"(?:什么|多少|哪些|何时|什么时候|如何|怎样|怎么|是否允许|能否|可否)"
)
_EXECUTION_COMMITMENT_CUE_PATTERN = re.compile(
    r"(?:我|我们|本人|真的|现在|立即|马上)"
)
_EXECUTION_COMMITMENT_WANT_ACTION_PATTERN = re.compile(
    r"(?<![需必重主次首])要\s*"
    r"(?:提交|发起|创建|撤回|撤销|批准|同意|通过|拒绝|驳回)"
)
_QUESTION_CLAUSE_PATTERN = re.compile(r"[^。；！？?\n]*[！？?]")
_POLICY_DETAIL_QUESTION_PATTERN = re.compile(
    r"(?:制度|规定|政策|权限|规则)[^，。；！？\n]{0,24}"
    r"(?:(?:什么|哪些|如何|怎么|吗|么)(?:[？?])?|[？?])"
)
_WRITE_REASON_BOUNDARY_PATTERN = re.compile(r"(?:理由|原因)\s*[:：]|因为")
_PROCUREMENT_POLICY_QUERY_PATTERN = re.compile(
    r"(?:采购)?金额[^，。；！？\n]{0,12}(?:达到|超过|高于|大于)"
    r"[^，。；！？\n]{0,12}(?:需要|必须|触发)"
    r"[^，。；！？\n]{0,12}(?:重新审批|审批|复审|复核)"
    r"|(?:采购)?(?:审批|复审|复核|重新审批)"
    r"[^，。；！？\n]{0,16}(?:金额|额度)"
    r"[^，。；！？\n]{0,12}(?:门槛|阈值|标准|限额|多少|几)"
    r"|(?:采购|审批|复审|复核|重新审批)"
    r"[^，。；！？\n]{0,16}"
    r"(?:金额门槛|金额阈值|金额标准|限额|额度标准|审批条件|复审条件)"
    r"|(?:采购|采购申请)[^，。；！？\n]{0,16}"
    r"(?:什么|哪些|何种)(?:条件|情况)(?:下)?"
    r"[^，。；！？\n]{0,16}(?:重新审批|复审|复核)"
    r"|(?:什么|哪些|何种)(?:条件|情况)(?:下)?"
    r"[^，。；！？\n]{0,16}(?:采购|采购申请)"
    r"[^，。；！？\n]{0,16}(?:重新审批|复审|复核)"
    r"|(?:采购|采购申请)[^，。；！？\n]{0,12}"
    r"(?:超过|达到|高于|大于)(?:多少|几)?(?:金额|额度)"
    r"[^，。；！？\n]{0,12}(?:需要|必须|触发)"
    r"[^，。；！？\n]{0,12}(?:重新审批|审批|复审|复核)"
)
_STANDALONE_APPROVAL_POLICY_QUERY_PATTERN = re.compile(
    r"(?:审批|复审|复核|重新审批)(?:的)?"
    r"(?:门槛|阈值|限额|条件|标准|规则)"
    r"[^，。；！？\n]{0,8}(?:多少|什么|哪些|如何|怎么|[？?])"
)
_NEUTRAL_POLICY_QUESTION_PATTERNS = (
    _POLICY_DETAIL_QUESTION_PATTERN,
    _PROCUREMENT_POLICY_QUERY_PATTERN,
    _STANDALONE_APPROVAL_POLICY_QUERY_PATTERN,
)
_LIST_MY_REQUESTS_PATTERN = re.compile(
    r"(?:列出|查看|查询)(?:一下)?我的(?:全部|所有)?(?:采购)?申请(?:列表|记录)?"
)
_DRAFT_REQUEST_PATTERN = re.compile(
    r"^\s*(?:(?:我|我们)(?:想|要|准备|计划)|(?:请|帮我|请帮我))\s*"
    r"(?:买|采购|购置)"
)
_APPROVE_COMMAND_PATTERN = re.compile(
    r"^\s*(?:(?:请|帮我|我要|现在|立即|确认)\s*)?"
    r"(?:批准|同意|通过)(?:这个|该|我的|采购|待审批|\s)*(?:任务|采购申请|申请)"
    r"|^\s*(?:(?:请|帮我|现在|立即|确认)\s*)?(?:把|将)"
    r"[^，。；！？\n]{0,32}(?:任务|采购申请|采购单)"
    r"[^，。；！？\n]{0,12}(?:批了|批准|通过|同意)"
    r"|^\s*我(?:要|决定|确认|现在|立即)\s*(?:批准|同意|通过)"
)
_REJECT_COMMAND_PATTERN = re.compile(
    r"^\s*(?:(?:请|帮我|我要|现在|立即|确认)\s*)?"
    r"(?:拒绝|驳回)(?:这个|该|我的|采购|待审批|\s)*(?:任务|采购申请|申请)"
    r"|^\s*(?:(?:请|帮我|现在|立即|确认)\s*)?(?:把|将)"
    r"[^，。；！？\n]{0,32}(?:任务|采购申请|采购单)"
    r"[^，。；！？\n]{0,12}(?:拒绝|驳回)"
    r"|^\s*我(?:要|决定|确认|现在|立即)\s*(?:拒绝|驳回)"
)
_SUBMIT_COMMAND_PATTERN = re.compile(
    r"^\s*(?:(?:请|帮我|我要|现在|立即|确认)\s*)?"
    r"(?:提交|发起|创建)(?:我的|一份|该|这个|新的|采购|\s)*(?:采购申请|申请)"
)
_WITHDRAW_COMMAND_PATTERN = re.compile(
    r"^\s*(?:(?:请|帮我|我要|现在|立即|确认)\s*)?"
    r"(?:撤回|撤销)(?:我的|该|这个|采购|\s)*(?:申请|采购单)"
)
_DIRECT_COMMAND_INTENTS = (
    (_APPROVE_COMMAND_PATTERN, "approve_task"),
    (_REJECT_COMMAND_PATTERN, "reject_task"),
    (_WITHDRAW_COMMAND_PATTERN, "withdraw_request"),
    (_SUBMIT_COMMAND_PATTERN, "submit_request"),
)
_CATEGORY_ALIASES = {
    "办公用品": "office_supplies",
    "office": "office_supplies",
    "it设备": "it_equipment",
    "it equipment": "it_equipment",
    "电脑设备": "it_equipment",
    "软件服务": "software_service",
    "software": "software_service",
    "专业服务": "professional_service",
    "professional service": "professional_service",
    "其他": "other",
}


class ProcurementToolFlowPolicy(DefaultToolFlowPolicy):
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
        self, user_text: str, context: ToolContext, registry: ToolRegistry
    ) -> ToolFlowState:
        intent = self._classify_intent(user_text)
        if intent == "unknown" and self._draft_intent in {
            "draft_request", "submit_request"
        }:
            intent = self._draft_intent
        policy_query_family = (
            self._policy_query_family(user_text) if intent == "policy" else "other"
        )
        collected: set[str] = set()
        server_supplied_argument_names: set[str] = set()
        server_supplied_argument_names.update(
            name
            for name in self._draft_fields
            if name in _VALIDATED_ARGUMENT_FIELDS
        )
        collected.update(server_supplied_argument_names)
        fact_flags: dict[str, str | bool] = {}
        if intent == "policy":
            fact_flags["policy_query_family"] = policy_query_family
        if intent == "unknown" and self._has_restricted_write_context(user_text):
            fact_flags["restricted_write"] = True
            fact_flags["security_refusal_required"] = True
        if intent == "reject_task" and "reason" not in collected:
            fact_flags["missing_required_fields"] = "reason"
        if intent == "draft_request":
            missing = sorted(_SUBMIT_FIELDS - collected)
            if missing:
                fact_flags["missing_required_fields"] = ",".join(missing)
            else:
                fact_flags["explicit_submit_required"] = True
        desired = self._initial_tools(intent, user_text, collected, fact_flags)
        authorized = self._authorized_names(context, registry)
        visible = tuple(name for name in desired if name in authorized)
        phase = FlowPhase.GATHERING if visible else FlowPhase.RESPOND_ONLY
        required_next_tool_name = (
            _CALCULATE
            if intent == "submit_request" and visible == (_CALCULATE,)
            else None
        )
        return ToolFlowState(
            phase=phase,
            intent=intent,
            visible_tool_names=visible,
            fact_flags=fact_flags,
            collected_argument_names=collected,
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
        normalized = {
            key: self._normalize_value(key, value)
            for key, value in arguments.items()
        }
        allowed_fields = set(definition.input_model.model_fields)
        for key, value in self._draft_fields.items():
            if key in allowed_fields:
                normalized[key] = self._normalize_value(key, value)
        if definition.name == _CALCULATE and "items" in normalized:
            normalized["items"] = self._calculation_items(normalized["items"])
        model_query = arguments.get("query")
        if definition.name == _POLICY and isinstance(model_query, str):
            trimmed_query = model_query.strip()
            if trimmed_query:
                family = state.fact_flags.get("policy_query_family")
                normalized["query"] = _CANONICAL_POLICY_QUERY_BY_FAMILY.get(
                    family if isinstance(family, str) else "other",
                    trimmed_query,
                )
        state.collected_argument_names.update(normalized)
        return normalized

    @staticmethod
    def _calculation_items(value: object) -> object:
        if not isinstance(value, list):
            return value
        return [
            {
                key: item[key]
                for key in ("quantity", "estimated_unit_price")
                if key in item
            }
            if isinstance(item, Mapping)
            else item
            for item in value
        ]

    @staticmethod
    def _policy_query_family(text: str) -> str:
        permission_question = _POLICY_PERMISSION_QUERY_PATTERN.search(text) is not None
        if (
            permission_question
            and _POLICY_PERMISSION_SCOPE_PATTERN.search(text) is not None
            and _POLICY_PERMISSION_ACTION_PATTERN.search(text) is not None
        ):
            return "delegated_bulk_permission"

        if (
            permission_question
            and any(term in text for term in ("撤回", "撤销"))
            and any(term in text for term in ("状态", "制度", "权限"))
        ):
            return "withdraw_status"

        if (
            any(term in text for term in ("金额", "额度"))
            and any(term in text for term in (
                "门槛", "阈值", "复审", "复核", "重新审批",
            ))
        ):
            return "amount_review"

        return "other"

    def observe_read(
        self,
        state: ToolFlowState,
        definition: ToolDefinition,
        _normalized_arguments: Mapping[str, object],
        model_result: Mapping[str, object],
    ) -> None:
        state.completed_read_names.add(definition.name)
        if definition.name in {_POLICY, _CALCULATE}:
            if definition.name == _CALCULATE and state.intent == "submit_request":
                state.fact_flags["calculated"] = True
                state.phase = FlowPhase.READY_TO_PROPOSE
                state.visible_tool_names = (_SUBMIT,)
                state.required_next_tool_name = _SUBMIT
            else:
                self._respond_only(state)
            return
        if definition.name == _LIST_REQUESTS:
            self._respond_only(state)
            return
        if definition.name == _GET_REQUEST:
            status = str(model_result.get("status", "unknown"))
            state.fact_flags["request_status"] = status
            if state.intent == "withdraw_request" and status in {
                "running", "pending_manager", "pending_procurement",
            }:
                state.phase = FlowPhase.READY_TO_PROPOSE
                state.visible_tool_names = (_WITHDRAW,)
            else:
                self._respond_only(state)
            return
        if definition.name == _LIST_TASKS:
            items = model_result.get("items", [])
            if not isinstance(items, list):
                self._respond_only(state)
                return
            pending = [
                item for item in items
                if isinstance(item, Mapping) and str(item.get("status")) == "pending"
            ]
            if state.intent in {"approve_task", "reject_task"}:
                if len(pending) == 1 and pending[0].get("task_id") is not None:
                    state.fact_flags["candidate_task_id"] = str(pending[0]["task_id"])
                    state.visible_tool_names = (_GET_TASK,)
                    state.phase = FlowPhase.GATHERING
                else:
                    state.fact_flags["clarification_required"] = True
                    state.fact_flags["candidate_count"] = str(len(pending))
                    self._respond_only(state)
            else:
                self._respond_only(state)
            return
        if definition.name == _GET_TASK:
            status = str(model_result.get("status", "unknown"))
            authorized = model_result.get("authorized") is True
            state.fact_flags["task_status"] = status
            state.fact_flags["task_authorized"] = authorized
            action = {
                "approve_task": _APPROVE,
                "reject_task": _REJECT,
            }.get(state.intent)
            reject_reason_ready = not (
                state.intent == "reject_task"
                and "reason" not in state.collected_argument_names
            )
            if (
                action is not None
                and status == "pending"
                and authorized
                and reject_reason_ready
            ):
                state.phase = FlowPhase.READY_TO_PROPOSE
                state.visible_tool_names = (action,)
            else:
                self._respond_only(state)

    @staticmethod
    def _respond_only(state: ToolFlowState) -> None:
        state.phase = FlowPhase.RESPOND_ONLY
        state.visible_tool_names = ()
        state.required_next_tool_name = None

    @staticmethod
    def _authorized_names(
        context: ToolContext, registry: ToolRegistry
    ) -> frozenset[str]:
        names: set[str] = set()
        for tool in registry.provider_tools(role=context.role):
            function = tool.get("function")
            if isinstance(function, Mapping) and isinstance(function.get("name"), str):
                names.add(registry.resolve_provider_name(function["name"]).name)
        return frozenset(names)

    def _initial_tools(
        self,
        intent: str,
        user_text: str,
        collected: set[str],
        fact_flags: dict[str, str | bool],
    ) -> tuple[str, ...]:
        if intent == "policy":
            return (_POLICY,)
        if intent == "calculate":
            return (_CALCULATE,)
        if intent == "list_requests":
            return (_LIST_REQUESTS,)
        if intent == "request_detail":
            return (_GET_REQUEST,) if "request_id" in collected else ()
        if intent == "submit_request":
            missing = sorted(_SUBMIT_FIELDS - collected)
            if missing:
                fact_flags["missing_required_fields"] = ",".join(missing)
                return ()
            return (_CALCULATE,)
        if intent == "withdraw_request":
            if "request_id" not in collected:
                fact_flags["missing_required_fields"] = "request_id"
                return ()
            return (_GET_REQUEST,)
        if intent == "list_tasks":
            return (_LIST_TASKS,)
        if intent == "task_detail":
            return (_GET_TASK,) if "task_id" in collected else ()
        if intent in {"approve_task", "reject_task"}:
            return (_GET_TASK,) if "task_id" in collected else (_LIST_TASKS,)
        return ()

    @staticmethod
    def _classify_intent(text: str) -> str:
        action = _WRITE_ACTION_PATTERN.search(text)
        policy_context = any(term in text for term in _POLICY_TERMS)
        direct_command = ProcurementToolFlowPolicy._match_direct_command(text)
        if direct_command is not None:
            direct_intent, command_match = direct_command
            if ProcurementToolFlowPolicy._has_restricted_write_context(text):
                return "unknown"
            if _EXPLICIT_WRITE_ACTION_NEGATION_PATTERN.search(command_match.group()):
                return "unknown"
            tail = text[command_match.end():]
            if _EXPLICIT_WRITE_ACTION_NEGATION_PATTERN.search(tail):
                return "unknown"
            if _DIRECT_TAIL_ACTION_UNCERTAINTY_PATTERN.search(tail):
                return "unknown"
            if _DIRECT_TAIL_FIRST_PERSON_HESITATION_PATTERN.search(tail):
                return "unknown"
            if _DIRECT_TAIL_DEICTIC_NEGATION_PATTERN.search(tail):
                return "unknown"
            if ProcurementToolFlowPolicy._has_execution_commitment_question(tail):
                return "unknown"
            neutral_tail = ProcurementToolFlowPolicy._strip_neutral_policy_questions(
                tail
            )
            if _UNSAFE_WRITE_QUESTION_PATTERN.search(neutral_tail):
                return "unknown"
            return direct_intent
        if policy_context and _POLICY_PERMISSION_QUERY_PATTERN.search(text):
            return "policy"
        if action is not None and ProcurementToolFlowPolicy._has_restricted_write_context(text):
            return "unknown"
        decision_text = _WRITE_REASON_BOUNDARY_PATTERN.split(text, maxsplit=1)[0]
        if action is not None and _UNSAFE_WRITE_CONTEXT_PATTERN.search(decision_text):
            return "unknown"
        if (
            policy_context
            or _PROCUREMENT_POLICY_QUERY_PATTERN.search(text)
            or _STANDALONE_APPROVAL_POLICY_QUERY_PATTERN.search(text)
        ):
            return "policy"
        if _DRAFT_REQUEST_PATTERN.search(text):
            return "draft_request"
        if any(term in text for term in ("计算", "算一下", "总额", "合计")):
            return "calculate"
        if "任务" in text and _UUID_PATTERN.search(text):
            return "task_detail"
        if any(term in text for term in ("待审批任务", "待办任务", "审批列表", "审批任务列表")):
            return "list_tasks"
        if ("申请" in text or "采购单" in text) and _UUID_PATTERN.search(text):
            return "request_detail"
        if (
            _LIST_MY_REQUESTS_PATTERN.search(text)
            or any(term in text for term in (
                "我的采购申请", "采购申请列表", "列出我的申请", "申请记录",
            ))
        ):
            return "list_requests"
        return "unknown"

    @staticmethod
    def _match_direct_command(text: str) -> tuple[str, re.Match[str]] | None:
        for pattern, intent in _DIRECT_COMMAND_INTENTS:
            match = pattern.search(text)
            if match is not None:
                return intent, match
        return None

    @staticmethod
    def _strip_neutral_policy_questions(text: str) -> str:
        stripped = text
        for pattern in _NEUTRAL_POLICY_QUESTION_PATTERNS:
            search_from = 0
            while (match := pattern.search(stripped, search_from)) is not None:
                end = match.end()
                if end < len(stripped) and stripped[end] in "？?":
                    end += 1
                candidate = stripped[match.start():end]
                if (
                    _INFORMATION_QUESTION_MARKER_PATTERN.search(candidate) is None
                    and not ProcurementToolFlowPolicy._is_neutral_policy_question(
                        candidate
                    )
                ):
                    search_from = match.end()
                    continue
                stripped = stripped[:match.start()] + stripped[end:]
                search_from = match.start()
        return stripped

    @staticmethod
    def _is_neutral_policy_question(text: str) -> bool:
        return (
            any(term in text for term in ("制度", "规定", "政策", "权限", "规则"))
            and _WRITE_ACTION_PATTERN.search(text) is not None
            and _UNSAFE_WRITE_QUESTION_PATTERN.search(text) is not None
            and _EXECUTION_COMMITMENT_CUE_PATTERN.search(text) is None
            and _EXECUTION_COMMITMENT_WANT_ACTION_PATTERN.search(text) is None
        )

    @staticmethod
    def _has_execution_commitment_question(text: str) -> bool:
        for match in _QUESTION_CLAUSE_PATTERN.finditer(text):
            clause = match.group()
            if _INFORMATION_QUESTION_MARKER_PATTERN.search(clause) is not None:
                continue
            if (
                (
                    _EXECUTION_COMMITMENT_CUE_PATTERN.search(clause) is not None
                    or _EXECUTION_COMMITMENT_WANT_ACTION_PATTERN.search(clause)
                    is not None
                )
                and _WRITE_ACTION_PATTERN.search(clause) is not None
            ):
                return True
        return False

    @staticmethod
    def _has_restricted_write_context(text: str) -> bool:
        return (
            _WRITE_ACTION_PATTERN.search(text) is not None
            and (
                _RESTRICTED_WRITE_CONTEXT_PATTERN.search(text) is not None
                or _OTHER_OWNER_CONTEXT_PATTERN.search(text) is not None
            )
        )

    def _normalize_value(self, key: str, value: object) -> object:
        if key in {"quantity", "estimated_unit_price"} and type(value) in {int, float}:
            return self._canonical_decimal(str(value))
        if isinstance(value, str):
            trimmed = value.strip()
            if key == "category_code":
                return _CATEGORY_ALIASES.get(trimmed.casefold(), trimmed)
            if key in {"quantity", "estimated_unit_price"}:
                return self._canonical_decimal(trimmed)
            return trimmed
        if isinstance(value, list):
            return [
                {
                    nested_key: self._normalize_value(nested_key, nested_value)
                    for nested_key, nested_value in item.items()
                }
                if isinstance(item, Mapping) else item
                for item in value
            ]
        return value

    @staticmethod
    def _canonical_decimal(value: str) -> str:
        try:
            number = Decimal(value)
        except InvalidOperation:
            return value
        if not number.is_finite():
            return value
        normalized = format(number, "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        return normalized or "0"


def build_procurement_tool_flow_policy(
    *,
    today_provider: Callable[[], date] = date.today,
    draft_fields: Mapping[str, object] | None = None,
    draft_intent: str | None = None,
) -> ProcurementToolFlowPolicy:
    return ProcurementToolFlowPolicy(
        today_provider=today_provider,
        draft_fields=draft_fields,
        draft_intent=draft_intent,
    )


__all__ = [
    "PROCUREMENT_INTENTS", "ProcurementToolFlowPolicy",
    "build_procurement_tool_flow_policy",
]
