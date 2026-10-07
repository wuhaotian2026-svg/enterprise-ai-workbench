"""Deterministic procurement API adapter over the domain service."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Any, Protocol
import uuid
import re
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, null, or_, select
from sqlalchemy.orm import Session
from pydantic import ValidationError

from policy_api.assistant_drafts.models import DraftStatus
from policy_api.assistant_drafts.store import AssistantDraftStore, DraftSnapshot
from policy_api.models import User
from policy_api.slot_extraction.controls import (
    parse_draft_control,
    validate_current_user_turn_text,
)
from policy_api.slot_extraction.errors import SlotExtractionError
from policy_api.slot_extraction.merge import CandidateValidationResult
from policy_api.slot_extraction.service import (
    PreparedExtraction,
    SlotExtractionPreparationRequest,
    SlotExtractionService,
)
from policy_api.approvals.enums import (
    ApprovalInstanceStatus,
    ApprovalTaskStatus,
    AssignmentKind,
)
from policy_api.approvals.models import ApprovalDecision, ApprovalInstance, ApprovalTask
from policy_api.approvals.runtime import ApprovalRuntime
from policy_api.approvals.schemas import ApprovalTaskSummary
from policy_api.approvals.subject_adapter import SubjectAdapterRegistry
from policy_api.procurement.models import AssistantTurn, ProcurementRequest
from policy_api.procurement.draft import (
    ProcurementDraftMerge,
    merge_procurement_draft,
    project_procurement_clarification,
)
from policy_api.procurement.draft_activation import (
    ProcurementDraftActivationPolicy,
    build_procurement_draft_activation_policy,
    has_procurement_draft_state,
)
from policy_api.procurement.calculation import calculate_total
from policy_api.procurement.conversations import ProcurementConversationStore
from policy_api.procurement.repository import ProcurementRepository
from policy_api.procurement.schemas import ProcurementRequestInput
from policy_api.procurement.slot_schema import PROCUREMENT_SLOT_SCHEMA
from policy_api.procurement.slot_validation import validate_procurement_candidates
from policy_api.procurement.service import (
    ProcurementApprovalAccess,
    ProcurementApprovalReadContext,
    ProcurementItemCommand,
    ProcurementRequestResult,
    ProcurementService,
    ProcurementSubjectAdapter,
    SubmitProcurementRequestCommand,
    procurement_display_status,
)
from policy_api.procurement.observability import (
    ProcurementObservability,
    attempt_outcome_for_code,
)
from policy_api.workbench.capabilities import Capability, CapabilityResolver
from policy_api.knowledge.tools import (
    PolicySearchOutcome,
    build_knowledge_tool_definition,
)
from policy_api.procurement.tool_flow_policy import (
    ProcurementToolFlowPolicy,
    build_procurement_tool_flow_policy,
)
from policy_api.procurement.prompt import (
    PROCUREMENT_POLICY_SEARCH_DESCRIPTION,
    PROCUREMENT_SYSTEM_MESSAGE,
)
from policy_api.procurement.tools import (
    ApproveTaskInput,
    RejectTaskInput,
    WithdrawRequestInput,
    build_procurement_tool_definitions,
    propose_procurement_write,
)
from policy_api.tools.definitions import ToolContext, ToolDefinition
from policy_api.tools.executor import ToolExecutor
from policy_api.tools.errors import ToolError
from policy_api.tools.orchestrator import BoundedToolOrchestrator, Planner
from policy_api.tools.registry import ToolRegistry
from policy_api.tools.schemas import (
    AssistantDraftBlock,
    ClarificationBlock,
    ConfirmationBlock,
    ErrorBlock,
    TextBlock,
    ToolTurnResponse,
)
from policy_api.tools.confirmation import (
    StagedToolExecutionExpired,
    ToolExecutionResource,
    cancel_tool_confirmation,
    compensate_failed_tool_execution,
    stage_tool_execution,
    canonical_arguments_hash,
    create_tool_confirmation,
)
from policy_api.tools.audit import AuditSummary
from policy_api.tools.enums import ToolInvocationStatus
from policy_api.tools.models import ToolConfirmation, ToolInvocation


DEFAULT_APPROVAL_RAW_SCAN_LIMIT = 200
_APPROVAL_ADVICE_PATTERN = re.compile(
    r"(?:建议|推荐|认为|判断)[^。！？.!?\n]{0,16}(?:批准|通过|同意|拒绝|驳回)"
    r"|(?:可以|可予|可|应该|应当|应予|最好)[^。！？.!?\n]{0,8}(?:批准|通过|同意|拒绝|驳回)"
    r"|(?:批准|通过|同意|拒绝|驳回)[^。！？.!?\n]{0,8}(?:更合适|为宜)"
    r"|(?:倾向于|适合|值得|不宜)[^。！？.!?\n]{0,8}(?:批准|通过|同意|拒绝|驳回)"
    r"|(?:批准|通过|同意|拒绝|驳回)[^。！？.!?\n]{0,16}(?:合理|适合|值得|不宜)"
    r"|具备[^。！？.!?\n]{0,8}(?:批准|通过|同意|拒绝|驳回)条件"
    r"|符合[^。！？.!?\n]{0,8}(?:批准|通过|同意|拒绝|驳回)条件"
    r"|\b(?:recommend(?:s|ed|ing)?\s+approv(?:e|es|ed|ing)|"
    r"advis(?:e|es|ed|ing)\s+(?:approv(?:e|es|ed|ing)|reject(?:s|ed|ing)?|approval|rejection)|"
    r"should\s+be\s+(?:approved|rejected)|can\s+be\s+(?:approved|rejected)|"
    r"fit\s+for\s+(?:approval|rejection)|"
    r"(?:approval|rejection)\s+(?:would\s+be|is)\s+(?:appropriate|warranted)|"
    r"(?:appropriate|warranted)\s+(?:to\s+(?:approve|reject)|for\s+(?:approval|rejection))|"
    r"warrants?\s+(?:approval|rejection)|"
    r"i\s+would\s+(?:approve|reject))\b",
    re.IGNORECASE,
)
_APPROVAL_PROCESS_PATTERN = re.compile(
    r"通过[^。！？.!?\n]{0,24}(?:审批|复核)(?:后|环节|流程)"
)
_HISTORICAL_ADVICE_PATTERN = re.compile(
    r"(?=[^。！？.!?\n]*(?:经理|采购专员|系统记录|审批记录))"
    r"(?=[^。！？.!?\n]*(?:曾|昨天|此前|随后|已经|已获|记录显示|显示))"
    r"|(?=[^。！？.!?\n]*(?:manager|reviewer|system\s+record|approval\s+record))"
    r"(?=[^。！？.!?\n]*(?:yesterday|previously|earlier|subsequently|already|recorded))",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY_PATTERN = re.compile(
    r"[，,；;]|(?:但是|并且|以及|同时|但)|\b(?:but|and)\b",
    re.IGNORECASE,
)
_SENTENCE_PATTERN = re.compile(r"[^。！？.!?\n]+(?:[。！？.!?]+|\n|$)|\n")
_NEUTRAL_APPROVAL_TEXT = (
    "我不能替审批人作出批准或拒绝决定；请您依据申请事实和制度自行判断。"
)
_INTERNAL_PLANNING_PATTERN = re.compile(
    r"(?:\bI need to\b|\bLet me\b|\bI should\b|\bActually\b|"
    r"\bfact_flags\b|\bcollected_argument_names\b|\bvisible_tool_names\b|"
    r"\bTRUSTED_FLOW_CONTROL\b|\bready_to_propose\b|\brespond_only\b)",
    re.IGNORECASE,
)


def procurement_text_postcondition(text: str) -> str:
    if _INTERNAL_PLANNING_PATTERN.search(text):
        return "请补充或确认当前采购草稿所需的信息。"
    guarded: list[str] = []
    for sentence in _SENTENCE_PATTERN.findall(text):
        current_clauses = (
            clause
            for clause in _CLAUSE_BOUNDARY_PATTERN.split(sentence)
            if not _HISTORICAL_ADVICE_PATTERN.search(clause)
        )
        advice_candidate = _APPROVAL_PROCESS_PATTERN.sub(
            "",
            " ".join(current_clauses),
        )
        if _APPROVAL_ADVICE_PATTERN.search(advice_candidate):
            leading_space = sentence[: len(sentence) - len(sentence.lstrip())]
            guarded.append(f"{leading_space}{_NEUTRAL_APPROVAL_TEXT}")
        else:
            guarded.append(sentence)
    return "".join(guarded)


_PROCUREMENT_DRAFT_FIELD_LABELS = {
    "title": "申请标题",
    "purpose": "采购用途",
    "needed_by_date": "需要日期",
    "needed_by_year": "需要日期年份",
    "currency": "币种",
    "items": "采购明细",
    "request_id": "采购申请编号",
    "task_id": "审批任务编号",
    "reason": "原因",
    "comment": "审批意见",
}
_PROCUREMENT_ITEM_LEAF_LABELS = {
    "item_name": "物品名称",
    "quantity": "数量",
    "unit": "计量单位",
    "estimated_unit_price": "预估单价",
    "category_code": "品类",
}
_PROCUREMENT_ITEM_MISSING_PATH = re.compile(
    r"^items\[(?P<index>\d+)\]\.(?P<field>[a-z_]+)$"
)
_PROCUREMENT_WHOLE_ITEM_MISSING_PATH = re.compile(
    r"^items\[(?P<index>\d+)\]$"
)
_PROCUREMENT_UNKNOWN_MISSING_LABEL = (
    "草稿中存在无法识别的信息状态，请清空草稿后重试"
)
_LOGGER = logging.getLogger(__name__)
_PROCUREMENT_REQUIRED_SUBMIT_FIELDS = frozenset(
    {"title", "purpose", "needed_by_date", "currency", "items"}
)
_PROCUREMENT_CLOSED_SUBMIT_CONTROLS = frozenset({"提交这份采购申请"})
_PROCUREMENT_DRAFT_INTENTS = frozenset({"draft_request", "submit_request"})
_PROCUREMENT_VALIDATED_ACTION_INTENTS = frozenset({
    "request_detail",
    "withdraw_request",
    "task_detail",
    "approve_task",
    "reject_task",
})


def _procurement_item_names(
    *,
    fields: Mapping[str, object],
    pending: Mapping[str, object],
) -> tuple[str | None, ...]:
    names: list[str | None] = []
    complete = fields.get("items")
    if isinstance(complete, list):
        for value in complete:
            name = value.get("item_name") if isinstance(value, Mapping) else None
            names.append(name.strip() if isinstance(name, str) and name.strip() else None)

    pending_items = pending.get("items")
    fragment = (
        pending_items.get("canonical_fragment")
        if isinstance(pending_items, Mapping)
        else None
    )
    if isinstance(fragment, Mapping):
        for collection_name in ("items", "unassigned_items"):
            collection = fragment.get(collection_name)
            if not isinstance(collection, list):
                continue
            for value in collection:
                item_fields = value.get("fields") if isinstance(value, Mapping) else None
                name = (
                    item_fields.get("item_name")
                    if isinstance(item_fields, Mapping)
                    else None
                )
                names.append(
                    name.strip()
                    if isinstance(name, str) and name.strip()
                    else None
                )
    return tuple(names)


def _procurement_missing_guidance(
    merge: ProcurementDraftMerge,
    *,
    missing_fields: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    item_names = _procurement_item_names(
        fields=merge.fields,
        pending=merge.pending,
    )
    guidance: list[str] = []
    grouped_item_fields: dict[int, list[str]] = {}
    actionable_missing = merge.missing_fields if missing_fields is None else missing_fields
    for name in actionable_missing:
        known_label = _PROCUREMENT_DRAFT_FIELD_LABELS.get(name)
        if known_label is not None:
            if known_label not in guidance:
                guidance.append(known_label)
            continue
        whole_item = _PROCUREMENT_WHOLE_ITEM_MISSING_PATH.fullmatch(name)
        if whole_item is not None:
            label = f"第 {int(whole_item.group('index')) + 1} 项采购明细"
            if label not in guidance:
                guidance.append(label)
            continue
        match = _PROCUREMENT_ITEM_MISSING_PATH.fullmatch(name)
        if match is None:
            path_kind = (
                "item_path_malformed"
                if name.startswith("items[")
                else "top_level_unknown"
            )
            _LOGGER.warning(
                "procurement_missing_path_unknown",
                extra={"path_kind": path_kind},
            )
            if _PROCUREMENT_UNKNOWN_MISSING_LABEL not in guidance:
                guidance.append(_PROCUREMENT_UNKNOWN_MISSING_LABEL)
            continue
        index = int(match.group("index"))
        leaf = _PROCUREMENT_ITEM_LEAF_LABELS.get(match.group("field"))
        if leaf is None:
            _LOGGER.warning(
                "procurement_missing_path_unknown",
                extra={"path_kind": "item_leaf_unknown"},
            )
            if _PROCUREMENT_UNKNOWN_MISSING_LABEL not in guidance:
                guidance.append(_PROCUREMENT_UNKNOWN_MISSING_LABEL)
            continue
        grouped_item_fields.setdefault(index, []).append(leaf)

    for index, labels in grouped_item_fields.items():
        unique_labels = tuple(dict.fromkeys(labels))
        item_name = item_names[index] if index < len(item_names) else None
        identity = f"第 {index + 1} 项"
        if item_name is not None:
            identity += f"（{item_name}）"
        guidance.append(f"{identity}的{'、'.join(unique_labels)}")
    return tuple(guidance)


def _with_procurement_draft_guidance(
    response: ToolTurnResponse,
    merge: ProcurementDraftMerge,
) -> ToolTurnResponse:
    """Keep employee guidance aligned with the trusted structured draft."""
    projection = project_procurement_clarification(
        missing_fields=merge.missing_fields,
        pending=merge.pending,
    )
    if (
        response.read_calls > 0
        or response.write_proposals > 0
        or any(block.type in {"error", "confirmation"} for block in response.blocks)
    ):
        return response
    if projection.missing_fields:
        labels = "、".join(
            _procurement_missing_guidance(
                merge,
                missing_fields=projection.missing_fields,
            )
        )
        text = (
            f"已记录本会话中你明确提供的采购信息，请只补充：{labels}。"
            "无需重复已经提供的内容。"
        )
    elif projection.pending_fields:
        labels = "、".join(
            _procurement_missing_guidance(
                merge,
                missing_fields=projection.pending_fields,
            )
        )
        text = f"以下信息仍需确认：{labels}。已验证的其他内容会继续保留。"
    elif not merge.explicit_submit:
        text = (
            "采购信息已齐全。如要办理，请发送“提交这份采购申请”；"
            "在你明确提交前不会生成确认卡。"
        )
    else:
        return response
    remaining = tuple(
        block for block in response.blocks if not isinstance(block, TextBlock)
    )
    return ToolTurnResponse(
        blocks=(TextBlock(text=text), *remaining),
        model_calls=response.model_calls,
        read_calls=response.read_calls,
        write_proposals=response.write_proposals,
        slot_extraction_calls=response.slot_extraction_calls,
    )


class ProcurementRequestReaderPort(Protocol):
    def page(
        self,
        db: Session,
        *,
        actor: User,
        status: str | None,
        submitted_from: date | None,
        submitted_to: date | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]: ...


class SqlAlchemyProcurementRequestReader:
    """Owner-scoped count/page reader for the synchronous procurement API."""

    def __init__(self, *, repository: ProcurementRepository) -> None:
        self._repository = repository

    def page(
        self,
        db: Session,
        *,
        actor: User,
        status: str | None,
        submitted_from: date | None,
        submitted_to: date | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        actor_id = getattr(actor, "id", None)
        if not isinstance(actor_id, uuid.UUID):
            raise ToolError("procurement_profile_required")
        authoritative_actor = self._repository.get_active_user(db, actor_id)
        if authoritative_actor is None:
            raise ToolError("procurement_profile_required")
        profile = self._repository.get_active_profile_by_user(
            db,
            authoritative_actor.id,
        )
        if profile is None:
            raise ToolError("procurement_profile_required")

        conditions = [
            ProcurementRequest.applicant_employee_id == profile.id,
            ApprovalInstance.applicant_user_id == authoritative_actor.id,
        ]
        if submitted_from is not None:
            conditions.append(
                ProcurementRequest.submitted_at
                >= datetime.combine(submitted_from, time.min, tzinfo=timezone.utc)
            )
        if submitted_to is not None and submitted_to != date.max:
            conditions.append(
                ProcurementRequest.submitted_at
                < datetime.combine(
                    submitted_to + timedelta(days=1),
                    time.min,
                    tzinfo=timezone.utc,
                )
            )
        if status == "pending_manager":
            conditions.extend(
                [
                    ApprovalInstance.status == ApprovalInstanceStatus.RUNNING,
                    ApprovalInstance.current_step_key
                    == "department_manager_review",
                ]
            )
        elif status == "pending_procurement":
            conditions.extend(
                [
                    ApprovalInstance.status == ApprovalInstanceStatus.RUNNING,
                    ApprovalInstance.current_step_key == "procurement_review",
                ]
            )
        elif status is not None:
            conditions.append(
                ApprovalInstance.status == ApprovalInstanceStatus(status)
            )

        total = db.scalar(
            select(func.count())
            .select_from(ProcurementRequest)
            .join(
                ApprovalInstance,
                ApprovalInstance.id == ProcurementRequest.approval_instance_id,
            )
            .where(*conditions)
        )
        rows = db.execute(
            select(ProcurementRequest, ApprovalInstance)
            .join(
                ApprovalInstance,
                ApprovalInstance.id == ProcurementRequest.approval_instance_id,
            )
            .where(*conditions)
            .order_by(
                ProcurementRequest.submitted_at.desc(),
                ProcurementRequest.id,
            )
            .offset(offset)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        items = []
        for request, instance in rows:
            summary = ProcurementSubjectAdapter.summary_from_request(
                request,
                instance,
            )
            items.append(
                {
                    "id": request.id,
                    "submitted_at": request.submitted_at,
                    **summary.model_dump(mode="json"),
                }
            )
        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "total": int(total or 0),
        }


class ProcurementRuntime:
    """Translate closed API input into the existing command service."""

    def __init__(
        self,
        *,
        service: ProcurementService,
        capability_resolver: CapabilityResolver,
        request_reader: ProcurementRequestReaderPort,
        conversation_store: ProcurementConversationStore | None = None,
        planner: Planner | None = None,
        slot_extraction: SlotExtractionService | None = None,
        approval_runtime: ApprovalApiRuntime | None = None,
        search_policy: Callable[[Session, str], PolicySearchOutcome] | None = None,
        max_model_calls: int = 3,
        max_read_calls: int = 4,
        confirmation_ttl_seconds: int = 600,
        observability: ProcurementObservability | None = None,
        draft_store: AssistantDraftStore | None = None,
        draft_activation_policy: ProcurementDraftActivationPolicy | None = None,
    ) -> None:
        self._service = service
        self.request_reader = request_reader
        self.capability_resolver = capability_resolver
        self.conversation_store = conversation_store or ProcurementConversationStore()
        self.planner = planner
        self.slot_extraction = slot_extraction
        self.approval_runtime = approval_runtime
        self.search_policy = search_policy or (
            lambda _db, _query: PolicySearchOutcome(
                status="refused",
                text=None,
                refusal_reason="policy_search_unavailable",
                citations=(),
            )
        )
        self.max_model_calls = max_model_calls
        self.max_read_calls = max_read_calls
        self.confirmation_ttl_seconds = confirmation_ttl_seconds
        self._observability = observability
        self.draft_store = draft_store or AssistantDraftStore()
        self.draft_activation_policy = (
            draft_activation_policy
            or build_procurement_draft_activation_policy()
        )

    def preview_request(
        self, request_input: ProcurementRequestInput
    ) -> dict[str, Any]:
        try:
            totals = calculate_total(request_input.items)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {
            "currency": request_input.currency.value,
            "subtotals": [format(value, ".2f") for value in totals.subtotals],
            "total": format(totals.total_amount, ".2f"),
        }

    def submit_request(
        self,
        db: Session,
        *,
        actor: User,
        client_operation_id: uuid.UUID,
        request_input: ProcurementRequestInput,
        request_id: str | None,
    ) -> dict[str, Any]:
        command = self._submission_command(request_input)
        result = self._service.submit_procurement_request(
            db,
            actor=actor,
            client_operation_id=client_operation_id,
            command=command,
            request_id=request_id,
            channel="manual",
        )
        return self._submission_payload(result)

    def confirm_submission(
        self,
        db: Session,
        *,
        actor: User,
        confirmation_id: uuid.UUID,
        client_operation_id: uuid.UUID,
        request_id: str | None,
    ) -> dict[str, Any]:
        actor_id = getattr(actor, "id", None)
        if not isinstance(actor_id, uuid.UUID):
            raise ToolError("confirmation_not_found")
        arguments = db.scalar(
            select(ToolConfirmation.normalized_arguments).where(
                ToolConfirmation.id == confirmation_id,
                ToolConfirmation.owner_user_id == actor_id,
            )
        )
        normalized_arguments = dict(arguments or {})
        confirmation_tool_name = db.scalar(
            select(ToolConfirmation.tool_name).where(
                ToolConfirmation.id == confirmation_id,
                ToolConfirmation.owner_user_id == actor_id,
            )
        )

        def execute(
            session: Session, confirmation: ToolConfirmation
        ) -> ToolExecutionResource:
            if confirmation.tool_name == "procurement.submit_request":
                try:
                    request_input = ProcurementRequestInput.model_validate(
                        confirmation.normalized_arguments
                    )
                except ValidationError:
                    raise ToolError("confirmation_arguments_invalid") from None
                result = self._service.submit_procurement_request(
                    session,
                    actor=actor,
                    client_operation_id=client_operation_id,
                    command=self._submission_command(request_input),
                    request_id=request_id,
                    channel="ai_confirmation",
                    commit=False,
                )
                return ToolExecutionResource(
                    resource_type="procurement_request",
                    resource_id=result.request.id,
                    result_summary={
                        "status": procurement_display_status(result.instance),
                        "request_number": result.request.request_number,
                    },
                    replayed=result.replayed,
                )
            if confirmation.tool_name == "procurement.withdraw_request":
                try:
                    parsed = WithdrawRequestInput.model_validate(
                        confirmation.normalized_arguments
                    )
                except ValidationError:
                    raise ToolError("confirmation_arguments_invalid") from None
                transition = self._service.withdraw_request(
                    session,
                    actor=actor,
                    request_id=parsed.request_id,
                    client_operation_id=client_operation_id,
                    audit_request_id=request_id,
                    channel="ai_confirmation",
                    commit=False,
                )
                return ToolExecutionResource(
                    resource_type="procurement_request",
                    resource_id=parsed.request_id,
                    result_summary={"status": transition.instance.status.value},
                    replayed=transition.replayed,
                )
            if confirmation.tool_name in {
                "approval.approve_task", "approval.reject_task",
            }:
                if self.approval_runtime is None:
                    raise ToolError("approval_runtime_unavailable")
                try:
                    if confirmation.tool_name == "approval.approve_task":
                        approve = ApproveTaskInput.model_validate(
                            confirmation.normalized_arguments
                        )
                        transition = self.approval_runtime.core_runtime.approve_task(
                            session,
                            actor=actor,
                            task_id=approve.task_id,
                            client_operation_id=client_operation_id,
                            comment=approve.comment,
                            request_id=request_id,
                            channel="ai_confirmation",
                            commit=False,
                        )
                    else:
                        reject = RejectTaskInput.model_validate(
                            confirmation.normalized_arguments
                        )
                        transition = self.approval_runtime.core_runtime.reject_task(
                            session,
                            actor=actor,
                            task_id=reject.task_id,
                            client_operation_id=client_operation_id,
                            reason=reject.reason,
                            request_id=request_id,
                            channel="ai_confirmation",
                            commit=False,
                        )
                except ValidationError:
                    raise ToolError("confirmation_arguments_invalid") from None
                procurement_request_id = session.scalar(
                    select(ProcurementRequest.id).where(
                        ProcurementRequest.approval_instance_id
                        == transition.instance.id
                    )
                )
                if procurement_request_id is None:
                    raise ToolError("procurement_request_not_found")
                return ToolExecutionResource(
                    resource_type="procurement_request",
                    resource_id=procurement_request_id,
                    result_summary={"status": transition.instance.status.value},
                    replayed=transition.replayed,
                )
            raise ToolError("confirmation_tool_mismatch")

        try:
            resource = stage_tool_execution(
                db,
                actor_user_id=actor_id,
                confirmation_id=confirmation_id,
                client_operation_id=client_operation_id,
                normalized_arguments=normalized_arguments,
                execute=execute,
            )
            if self._observability is not None and resource.replayed:
                if request_id is not None and confirmation_tool_name is not None:
                    stage = {
                        "procurement.submit_request": "submission",
                        "procurement.withdraw_request": "withdrawal",
                        "approval.approve_task": None,
                        "approval.reject_task": None,
                    }[confirmation_tool_name]
                    task_value = normalized_arguments.get("task_id")
                    approval_task_id = (
                        uuid.UUID(str(task_value))
                        if confirmation_tool_name.startswith("approval.")
                        and task_value is not None
                        else None
                    )
                    self._observability.stage_attempt(
                        db,
                        actor_user_id=actor_id,
                        actor_role=actor.role.value,
                        client_operation_id=client_operation_id,
                        server_attempt_id=request_id,
                        command_kind=confirmation_tool_name,
                        outcome="replayed",
                        code="exact_replay",
                        stage=stage,
                        request_id=resource.resource_id,
                        approval_task_id=approval_task_id,
                    )
            elif self._observability is not None:
                self._observability.stage_confirmation(
                    db, actor_user_id=actor_id, actor_role=actor.role.value,
                    confirmation_id=confirmation_id,
                    event_name="procurement_confirmation_confirmed",
                    tool_name=confirmation_tool_name or "procurement.submit_request",
                    request_id=request_id,
                    procurement_request_id=resource.resource_id,
                )
            db.commit()
        except StagedToolExecutionExpired:
            try:
                if self._observability is not None and confirmation_tool_name is not None:
                    self._observability.stage_confirmation(
                        db, actor_user_id=actor_id, actor_role=actor.role.value,
                        confirmation_id=confirmation_id,
                        event_name="procurement_confirmation_expired",
                        tool_name=confirmation_tool_name, request_id=request_id,
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
            raise
        except ToolError as exc:
            db.rollback()
            if (
                self._observability is not None
                and request_id is not None
                and confirmation_tool_name is not None
                and attempt_outcome_for_code(exc.code) is not None
            ):
                outcome = attempt_outcome_for_code(exc.code)
                assert outcome is not None
                stage = {
                    "procurement.submit_request": "submission",
                    "procurement.withdraw_request": "withdrawal",
                    "approval.approve_task": None,
                    "approval.reject_task": None,
                }[confirmation_tool_name]
                task_value = normalized_arguments.get("task_id")
                approval_task_id = (
                    uuid.UUID(str(task_value))
                    if confirmation_tool_name.startswith("approval.")
                    and task_value is not None
                    else None
                )
                try:
                    self._observability.stage_attempt(
                        db, actor_user_id=actor_id, actor_role=actor.role.value,
                        client_operation_id=client_operation_id,
                        server_attempt_id=request_id,
                        command_kind=confirmation_tool_name, outcome=outcome,
                        code=exc.code, stage=stage,
                        approval_task_id=approval_task_id,
                    )
                    db.commit()
                except Exception as evidence_error:
                    db.rollback()
                    exc.add_note(f"procurement evidence failed: {type(evidence_error).__name__}")
            raise
        except Exception:
            db.rollback()
            confirmation = db.scalar(
                select(ToolConfirmation).where(
                    ToolConfirmation.id == confirmation_id,
                    ToolConfirmation.owner_user_id == actor_id,
                )
            )
            if confirmation is not None:
                compensate_failed_tool_execution(
                    db,
                    invocation_id=confirmation.invocation_id,
                    confirmation_id=confirmation.id,
                    actor_user_id=actor_id,
                    summary=AuditSummary.for_execution(
                        tool_name=confirmation.tool_name,
                        risk_level="write",
                        outcome="failed",
                    ),
                )
            raise ToolError("tool_execution_failed") from None
        return {
            "type": "execution_result",
            "resource_type": resource.resource_type,
            "resource_id": resource.resource_id,
            "replayed": resource.replayed,
        }

    def create_conversation(
        self, db: Session, actor_user_id: uuid.UUID, title: str | None
    ) -> dict[str, object]:
        return self.conversation_store.create(
            db, owner_user_id=actor_user_id, title=title
        )

    def cancel_confirmation(
        self,
        db: Session,
        actor_user_id: uuid.UUID,
        confirmation_id: uuid.UUID,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        confirmation_tool_name = db.scalar(
            select(ToolConfirmation.tool_name).where(
                ToolConfirmation.id == confirmation_id,
                ToolConfirmation.owner_user_id == actor_user_id,
            )
        )
        try:
            confirmation = cancel_tool_confirmation(
                db, actor_user_id, confirmation_id
            )
            if self._observability is not None:
                actor = db.get(User, actor_user_id)
                self._observability.stage_confirmation(
                    db, actor_user_id=actor_user_id,
                    actor_role=actor.role.value if actor is not None else "system",
                    confirmation_id=confirmation.id,
                    event_name="procurement_confirmation_cancelled",
                    tool_name=confirmation.tool_name,
                    request_id=request_id,
                )
            db.commit()
        except StagedToolExecutionExpired:
            try:
                if self._observability is not None and confirmation_tool_name is not None:
                    actor = db.get(User, actor_user_id)
                    self._observability.stage_confirmation(
                        db, actor_user_id=actor_user_id,
                        actor_role=actor.role.value if actor is not None else "system",
                        confirmation_id=confirmation_id,
                        event_name="procurement_confirmation_expired",
                        tool_name=confirmation_tool_name,
                        request_id=request_id,
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
            raise
        except Exception:
            db.rollback()
            raise
        return {
            "confirmation_id": confirmation.id,
            "status": confirmation.status.value,
        }

    def list_conversations(
        self, db: Session, actor_user_id: uuid.UUID
    ) -> list[dict[str, object]]:
        return self.conversation_store.list(db, owner_user_id=actor_user_id)

    def get_conversation(
        self, db: Session, actor_user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> dict[str, object]:
        return self.conversation_store.get(
            db,
            owner_user_id=actor_user_id,
            conversation_id=conversation_id,
        )

    def run_turn(
        self,
        db: Session,
        actor_user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        client_turn_id: uuid.UUID,
        text: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        stored = self.conversation_store.add_turn(
            db,
            owner_user_id=actor_user_id,
            conversation_id=conversation_id,
            client_turn_id=client_turn_id,
            text=text,
        )
        if self.planner is None:
            return {**stored, "slot_extraction_calls": 0}
        if stored.get("replayed") is True:
            return {
                **stored,
                "model_calls": 0,
                "read_calls": 0,
                "write_proposals": 0,
                "slot_extraction_calls": 0,
            }
        turn_id = stored.get("id")
        turn = db.get(AssistantTurn, turn_id) if isinstance(turn_id, uuid.UUID) else None
        actor = db.get(User, actor_user_id)
        if turn is None or actor is None or not actor.is_active:
            raise ToolError("authentication_required")
        try:
            validate_current_user_turn_text(text)
        except SlotExtractionError:
            raise ToolError("request_validation_failed") from None

        preexisting_draft = self.draft_store.get_active(
            db,
            owner_user_id=actor_user_id,
            module_key="procurement",
            conversation_id=conversation_id,
            for_update=False,
        )
        pending_conflict_names = tuple(
            name
            for name, value in (
                preexisting_draft.pending.items() if preexisting_draft else ()
            )
            if isinstance(value, Mapping)
            and value.get("reason_code") == "draft_value_conflict"
        )
        control = parse_draft_control(
            text,
            pending_conflict_names=pending_conflict_names,
            field_labels=_PROCUREMENT_DRAFT_FIELD_LABELS,
        )
        preexisting_complete = bool(
            preexisting_draft is not None
            and _PROCUREMENT_REQUIRED_SUBMIT_FIELDS
            <= set(preexisting_draft.fields)
            and not preexisting_draft.pending
        )
        closed_submit_control = (
            text.strip() in _PROCUREMENT_CLOSED_SUBMIT_CONTROLS
            and preexisting_complete
        )
        current_intent = ProcurementToolFlowPolicy._classify_intent(text)
        prepared: PreparedExtraction | None = None
        validation = CandidateValidationResult(
            accepted={}, pending={}, rejected=()
        )
        slot_extraction_calls = 0
        procurement_today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if control is None and not closed_submit_control:
            if self.slot_extraction is None:
                return self._persist_assistant_turn(
                    db,
                    stored,
                    turn,
                    self._slot_extraction_failure_response(
                        "slot_extraction_not_configured",
                        slot_extraction_calls=0,
                    ),
                )
            try:
                prepared = self.slot_extraction.prepare(
                    db,
                    SlotExtractionPreparationRequest(
                        owner_user_id=actor_user_id,
                        module_key="procurement",
                        conversation_id=conversation_id,
                        client_turn_id=client_turn_id,
                        current_user_turn_text=text,
                        slot_schema=PROCUREMENT_SLOT_SCHEMA,
                    ),
                    lambda current_text, envelope: validate_procurement_candidates(
                        text=current_text,
                        envelope=envelope,
                        today=procurement_today,
                        source_turn_id=str(turn.id),
                    ),
                )
            except SlotExtractionError as exc:
                return self._persist_assistant_turn(
                    db,
                    stored,
                    turn,
                    self._slot_extraction_failure_response(
                        exc.code,
                        slot_extraction_calls=exc.slot_extraction_calls,
                        retryable=exc.retryable,
                    ),
                )
            validation = prepared.dispositions
            slot_extraction_calls = prepared.slot_extraction_calls

        active_draft = self.draft_store.get_active(
            db,
            owner_user_id=actor_user_id,
            module_key="procurement",
            conversation_id=conversation_id,
            for_update=True,
        )
        explicit_submit = (
            current_intent == "submit_request"
            or text.strip() in _PROCUREMENT_CLOSED_SUBMIT_CONTROLS
        )
        try:
            draft_merge = merge_procurement_draft(
                fields=active_draft.fields if active_draft else {},
                pending=active_draft.pending if active_draft else {},
                sources=active_draft.sources if active_draft else {},
                validation=validation,
                control=control,
                source_turn_id=str(turn.id),
                explicit_submit=explicit_submit,
                today=procurement_today,
            )
        except SlotExtractionError as exc:
            db.rollback()
            if prepared is not None and self.slot_extraction is not None:
                self.slot_extraction.repository.complete_failure(
                    db,
                    prepared.operation_id,
                    error_code=exc.code,
                    latency_ms=prepared.latency_ms,
                    now=datetime.now(timezone.utc),
                )
                db.commit()
            return self._persist_assistant_turn(
                db,
                stored,
                turn,
                self._slot_extraction_failure_response(
                    exc.code,
                    slot_extraction_calls=slot_extraction_calls,
                    retryable=exc.retryable,
                ),
            )
        if draft_merge.clear_requested:
            self.draft_store.transition(
                db,
                owner_user_id=actor_user_id,
                module_key="procurement",
                conversation_id=conversation_id,
                status=DraftStatus.CLEARED,
            )
            response = ToolTurnResponse(
                blocks=(TextBlock(text="已清空当前采购办事草稿。"),),
                model_calls=0,
                read_calls=0,
                write_proposals=0,
                slot_extraction_calls=slot_extraction_calls,
            )
            return self._persist_assistant_turn(db, stored, turn, response)

        if prepared is not None and self.slot_extraction is not None:
            try:
                self.slot_extraction.complete_success(db, prepared)
            except SlotExtractionError as exc:
                db.commit()
                return self._persist_assistant_turn(
                    db,
                    stored,
                    turn,
                    self._slot_extraction_failure_response(
                        exc.code,
                        slot_extraction_calls=prepared.slot_extraction_calls,
                        retryable=exc.retryable,
                    ),
                )

        activation = self.draft_activation_policy.decide(
            current_intent=current_intent,
            has_active_draft=(
                active_draft is not None
                and has_procurement_draft_state(
                    fields=active_draft.fields,
                    pending=active_draft.pending,
                )
            ),
            validation=validation,
        )
        effective_intent = activation.effective_intent
        draft_snapshot: DraftSnapshot | None = active_draft
        if activation.should_save:
            draft_snapshot = self.draft_store.save(
                db,
                owner_user_id=actor_user_id,
                module_key="procurement",
                conversation_id=conversation_id,
                intent="draft_request",
                fields=draft_merge.fields,
                sources=draft_merge.sources,
                pending=draft_merge.pending,
            )

        if (
            draft_snapshot is not None
            and effective_intent in _PROCUREMENT_DRAFT_INTENTS
            and (
                draft_merge.missing_fields
                or draft_merge.pending
                or validation.pending_count > 0
                or validation.rejected_count > 0
                or not explicit_submit
            )
        ):
            response = self._procurement_draft_response(
                draft_snapshot,
                draft_merge,
                validation,
                slot_extraction_calls=slot_extraction_calls,
            )
            return self._persist_assistant_turn(db, stored, turn, response)

        if (
            effective_intent in _PROCUREMENT_VALIDATED_ACTION_INTENTS
            and (validation.pending_count > 0 or validation.rejected_count > 0)
        ):
            response = self._procurement_validation_response(
                validation,
                slot_extraction_calls=slot_extraction_calls,
            )
            return self._persist_assistant_turn(db, stored, turn, response)

        db.commit()
        response = self._orchestrator(
            db,
            turn,
            request_id=request_id,
            draft_snapshot=draft_snapshot,
            draft_intent=(
                "submit_request" if explicit_submit else effective_intent
            ),
        ).run(
            text,
            ToolContext(actor_user_id=actor_user_id, role=actor.role),
        )
        response = self._with_slot_extraction_calls(
            response,
            slot_extraction_calls,
        )
        if draft_snapshot is not None:
            if (
                explicit_submit
                and any(
                    isinstance(block, ConfirmationBlock)
                    for block in response.blocks
                )
            ):
                self.draft_store.transition(
                    db,
                    owner_user_id=actor_user_id,
                    module_key="procurement",
                    conversation_id=conversation_id,
                    status=DraftStatus.CLOSED,
                )
            elif effective_intent in _PROCUREMENT_DRAFT_INTENTS:
                response = _with_procurement_draft_guidance(
                    response,
                    draft_merge,
                )
                response = self._with_draft_block(
                    response,
                    draft_snapshot,
                    draft_merge,
                )
        return self._persist_assistant_turn(db, stored, turn, response)

    def _orchestrator(
        self,
        db: Session,
        turn: AssistantTurn | None,
        *,
        request_id: str | None = None,
        draft_snapshot: DraftSnapshot | None = None,
        draft_intent: str | None = None,
    ) -> BoundedToolOrchestrator:
        if self.planner is None:
            raise ToolError("tool_planner_unavailable")
        approval_runtime = self.approval_runtime
        if approval_runtime is None:
            raise ToolError("approval_runtime_unavailable")
        definitions = (
            build_knowledge_tool_definition(
                lambda query: self.search_policy(db, query),
                description=PROCUREMENT_POLICY_SEARCH_DESCRIPTION,
            ),
            *build_procurement_tool_definitions(
                db,
                procurement_runtime=self,
                approval_runtime=approval_runtime,
            ),
        )
        registry = ToolRegistry(definitions)

        def persist(
            tool_name: str,
            normalized: dict[str, object],
            preview: dict[str, object],
            context: ToolContext,
        ) -> ConfirmationBlock:
            if turn is None:
                raise ToolError("tool_turn_required")
            definition = registry.get(tool_name)
            invocation = ToolInvocation(
                conversation_id=turn.conversation_id,
                turn_id=turn.id,
                actor_user_id=context.actor_user_id,
                provider_call_id=f"procurement-runtime-{uuid.uuid4()}",
                tool_name=definition.name,
                provider_tool_name=registry.provider_name(definition.name),
                risk_level=definition.risk_level.value,
                status=ToolInvocationStatus.PROPOSED,
                arguments_hash=canonical_arguments_hash(normalized),
            )
            db.add(invocation)
            db.flush()
            confirmation = create_tool_confirmation(
                db,
                invocation=invocation,
                owner_user_id=context.actor_user_id,
                normalized_arguments=normalized,
                preview=preview,
                expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=self.confirmation_ttl_seconds),
                audit_summary=AuditSummary.for_execution(
                    tool_name=definition.name,
                    risk_level=definition.risk_level.value,
                    outcome="confirmation_pending",
                ),
            )
            if self._observability is not None:
                actor = db.get(User, context.actor_user_id)
                self._observability.stage_confirmation(
                    db, actor_user_id=context.actor_user_id,
                    actor_role=actor.role.value if actor is not None else "system",
                    confirmation_id=confirmation.id,
                    event_name="procurement_confirmation_shown",
                    tool_name=confirmation.tool_name,
                    request_id=request_id,
                )
            return ConfirmationBlock(
                confirmation_id=confirmation.id,
                tool_name=confirmation.tool_name,
                preview=dict(confirmation.preview),
                expires_at=confirmation.expires_at,
            )

        def propose_write(
            definition: ToolDefinition,
            arguments: Mapping[str, object],
            context: ToolContext,
        ) -> ConfirmationBlock:
            if draft_snapshot is not None:
                self.draft_store.assert_current_version(
                    db,
                    draft_id=draft_snapshot.id,
                    expected_version=draft_snapshot.version,
                )
            return propose_procurement_write(
                db,
                definition,
                arguments,
                context,
                procurement_runtime=self,
                approval_runtime=approval_runtime,
                persist=persist,
            )

        return BoundedToolOrchestrator(
            planner=self.planner,
            registry=registry,
            executor=ToolExecutor(registry),
            propose_write=propose_write,
            max_model_calls=self.max_model_calls,
            max_read_calls=self.max_read_calls,
            max_write_proposals=1,
            flow_policy=build_procurement_tool_flow_policy(
                draft_fields=draft_snapshot.fields if draft_snapshot else None,
                draft_intent=draft_intent,
            ),
            system_message=(
                PROCUREMENT_SYSTEM_MESSAGE
                + " 只能输出简洁、面向员工的中文最终回复；不得输出分析过程、内部规划、"
                "英文思考、flow-control、fact_flags 或工具 schema。"
            ),
            text_postcondition=procurement_text_postcondition,
        )

    def _persist_assistant_turn(
        self,
        db: Session,
        stored: Mapping[str, object],
        turn: AssistantTurn,
        response: ToolTurnResponse,
    ) -> dict[str, object]:
        blocks = [item.model_dump(mode="json") for item in response.blocks]
        assistant_text = next(
            (
                block.text
                for block in response.blocks
                if isinstance(block, TextBlock) and block.text.strip()
            ),
            "",
        )
        turn.role = "assistant"
        turn.content = assistant_text
        turn.blocks = blocks
        turn.model_name = getattr(self.planner, "model", None)
        db.commit()
        return {
            **stored,
            "role": "assistant",
            "text": assistant_text,
            "blocks": tuple(blocks),
            "model_calls": response.model_calls,
            "read_calls": response.read_calls,
            "write_proposals": response.write_proposals,
            "slot_extraction_calls": response.slot_extraction_calls,
        }

    @staticmethod
    def _slot_extraction_failure_response(
        code: str,
        *,
        slot_extraction_calls: int,
        retryable: bool = True,
    ) -> ToolTurnResponse:
        return ToolTurnResponse(
            blocks=(
                TextBlock(
                    text=(
                        "本轮信息未被保存。请使用一条新消息重试，"
                        "无需重复此前已经验证并保存的草稿信息。"
                    )
                ),
                ErrorBlock(code=code, retryable=retryable),
            ),
            model_calls=0,
            read_calls=0,
            write_proposals=0,
            slot_extraction_calls=slot_extraction_calls,
        )

    @staticmethod
    def _with_slot_extraction_calls(
        response: ToolTurnResponse,
        slot_extraction_calls: int,
    ) -> ToolTurnResponse:
        return ToolTurnResponse(
            blocks=response.blocks,
            model_calls=response.model_calls,
            read_calls=response.read_calls,
            write_proposals=response.write_proposals,
            slot_extraction_calls=slot_extraction_calls,
        )

    @staticmethod
    def _procurement_draft_response(
        snapshot: DraftSnapshot,
        merge: ProcurementDraftMerge,
        validation: CandidateValidationResult,
        *,
        slot_extraction_calls: int,
    ) -> ToolTurnResponse:
        projection = project_procurement_clarification(
            missing_fields=merge.missing_fields,
            pending=merge.pending,
        )
        if projection.missing_fields:
            labels = "、".join(
                _procurement_missing_guidance(
                    merge,
                    missing_fields=projection.missing_fields,
                )
            )
            text = (
                f"已记录本会话中你明确提供的采购信息，请只补充：{labels}。"
                "无需重复已经提供的内容。"
            )
        elif projection.pending_fields:
            labels = "、".join(
                _procurement_missing_guidance(
                    merge,
                    missing_fields=projection.pending_fields,
                )
            )
            text = f"以下信息仍需确认：{labels}。已验证的其他内容会继续保留。"
        elif validation.rejected_count > 0:
            text = "本轮部分信息未通过校验，请按提示重新提供对应字段。"
        else:
            text = (
                "采购信息已齐全。如要办理，请发送“提交这份采购申请”；"
                "在你明确提交前不会生成确认卡。"
            )
        response = ToolTurnResponse(
            blocks=(
                TextBlock(text=text),
                ClarificationBlock(
                    missing_fields=projection.clarification_fields,
                    suggestions=(),
                ),
            ),
            model_calls=0,
            read_calls=0,
            write_proposals=0,
            slot_extraction_calls=slot_extraction_calls,
        )
        return ProcurementRuntime._with_draft_block(
            response,
            snapshot,
            merge,
        )

    @staticmethod
    def _procurement_validation_response(
        validation: CandidateValidationResult,
        *,
        slot_extraction_calls: int,
    ) -> ToolTurnResponse:
        names = tuple(sorted(validation.pending))
        return ToolTurnResponse(
            blocks=(
                TextBlock(text="本轮部分信息仍需补充或重新确认。"),
                ClarificationBlock(
                    missing_fields=names,
                    suggestions=(),
                ),
            ),
            model_calls=0,
            read_calls=0,
            write_proposals=0,
            slot_extraction_calls=slot_extraction_calls,
        )

    @staticmethod
    def _with_draft_block(
        response: ToolTurnResponse,
        snapshot: DraftSnapshot,
        merge: ProcurementDraftMerge,
    ) -> ToolTurnResponse:
        projection = project_procurement_clarification(
            missing_fields=merge.missing_fields,
            pending=merge.pending,
        )
        block = AssistantDraftBlock(
            module_key="procurement",
            intent=merge.intent,
            status="active",
            version=snapshot.version,
            fields=dict(snapshot.fields),
            pending_fields=projection.pending_fields,
            missing_fields=projection.missing_fields,
        )
        return ToolTurnResponse(
            blocks=(*response.blocks, block),
            model_calls=response.model_calls,
            read_calls=response.read_calls,
            write_proposals=response.write_proposals,
            slot_extraction_calls=response.slot_extraction_calls,
        )

    @staticmethod
    def _submission_command(
        request_input: ProcurementRequestInput,
    ) -> SubmitProcurementRequestCommand:
        return SubmitProcurementRequestCommand(
            title=request_input.title,
            purpose=request_input.purpose,
            needed_by_date=request_input.needed_by_date,
            currency=request_input.currency,
            items=tuple(
                ProcurementItemCommand(
                    category_code=item.category_code,
                    item_name=item.item_name,
                    specification=item.specification,
                    quantity=item.quantity,
                    unit=item.unit,
                    estimated_unit_price=item.estimated_unit_price,
                )
                for item in request_input.items
            ),
        )

    def list_requests(
        self,
        db: Session,
        *,
        actor: User,
        status: str | None,
        submitted_from: date | None,
        submitted_to: date | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        return self.request_reader.page(
            db,
            actor=actor,
            status=status,
            submitted_from=submitted_from,
            submitted_to=submitted_to,
            offset=offset,
            limit=limit,
        )

    def get_request(
        self, db: Session, *, actor: User, request_id: uuid.UUID
    ) -> dict[str, Any]:
        detail = self._service.get_my_request(
            db, actor=actor, request_id=request_id
        )
        return {"id": request_id, **detail.model_dump(mode="json")}

    def preflight_submit(self, db: Session, *, actor: User) -> object:
        return self._service.preflight_submit(db, actor=actor)

    def withdraw_request(
        self,
        db: Session,
        *,
        actor: User,
        request_id: uuid.UUID,
        client_operation_id: uuid.UUID,
        request_trace_id: str | None = None,
    ) -> dict[str, Any]:
        transition = self._service.withdraw_request(
            db,
            actor=actor,
            request_id=request_id,
            client_operation_id=client_operation_id,
            audit_request_id=request_trace_id,
            channel="manual",
        )
        return {
            "instance_id": transition.instance.id,
            "status": transition.instance.status.value,
            "current_step_key": transition.instance.current_step_key,
            "replayed": transition.replayed,
        }

    @staticmethod
    def _submission_payload(result: ProcurementRequestResult) -> dict[str, Any]:
        return {
            "id": result.request.id,
            "request_number": result.request.request_number,
            "title": result.request.title,
            "total": result.request.total_amount,
            "status": procurement_display_status(result.instance),
            "submitted_at": result.request.submitted_at,
            "replayed": result.replayed,
        }


@dataclass(frozen=True, slots=True)
class ApprovalTaskCandidate:
    task: ApprovalTask
    instance: ApprovalInstance
    decision: ApprovalDecision | None
    read_context: ProcurementApprovalReadContext | None

    def __iter__(self):
        yield self.task
        yield self.instance
        yield self.decision


class ApprovalTaskReaderPort(Protocol):
    def candidates(
        self,
        db: Session,
        *,
        actor: User,
        status: str | None,
        process_key: str | None,
        activated_from: datetime | None,
        activated_to: datetime | None,
    ) -> Iterable[ApprovalTaskCandidate]: ...

    def summary(
        self,
        db: Session,
        *,
        actor: User,
        candidate: ApprovalTaskCandidate,
    ) -> ApprovalTaskSummary | None: ...


class SqlAlchemyApprovalTaskReader:
    """Streams raw approval candidates through the public Task 8 ports."""

    def __init__(
        self,
        *,
        access: ProcurementApprovalAccess,
        registry: SubjectAdapterRegistry,
    ) -> None:
        self._access = access
        self._registry = registry

    def candidates(
        self,
        db: Session,
        *,
        actor: User,
        status: str | None,
        process_key: str | None,
        activated_from: datetime | None,
        activated_to: datetime | None,
    ) -> Iterable[ApprovalTaskCandidate]:
        actor_id = getattr(actor, "id", None)
        explicit_pending = status == ApprovalTaskStatus.PENDING.value
        if explicit_pending:
            visibility_conditions = [
                ApprovalTask.assigned_user_id == actor_id
            ]
        else:
            assigned_task_visibility = ApprovalTask.__table__.alias(
                "assigned_task_visibility"
            )
            assigned_task_ids = select(
                assigned_task_visibility.c.id
            ).where(
                assigned_task_visibility.c.assigned_user_id == actor_id
            )
            if status is not None:
                assigned_task_ids = assigned_task_ids.where(
                    assigned_task_visibility.c.status
                    == ApprovalTaskStatus(status)
                )
            actor_visible_task_ids = assigned_task_ids.union(
                select(ApprovalDecision.task_id).where(
                    ApprovalDecision.actor_user_id == actor_id
                )
            )
            visibility_conditions = [
                ApprovalTask.id.in_(actor_visible_task_ids)
            ]
        read_context: ProcurementApprovalReadContext | None = None
        if status in {None, ApprovalTaskStatus.PENDING.value}:
            read_context = self._access.read_context(
                db,
                actor=actor,
            )
            capability_scope = read_context.final_review_scope
            if capability_scope is not None and (
                capability_scope.is_global
                or capability_scope.organization_unit_ids
            ):
                capability_condition = and_(
                    ApprovalTask.status == ApprovalTaskStatus.PENDING,
                    ApprovalTask.assignment_kind == AssignmentKind.CAPABILITY,
                    ApprovalTask.assigned_user_id.is_(None),
                    ApprovalTask.required_capability
                    == Capability.PROCUREMENT_FINAL_REVIEW.value,
                    ApprovalTask.scope_organization_unit_id.is_not(None),
                    ApprovalTask.scope_organization_unit_id
                    == ApprovalInstance.organization_unit_id,
                )
                if not capability_scope.is_global:
                    capability_condition = and_(
                        capability_condition,
                        ApprovalTask.scope_organization_unit_id.in_(
                            capability_scope.organization_unit_ids
                        ),
                    )
                visibility_conditions.append(capability_condition)
        if explicit_pending:
            statement = select(
                ApprovalTask,
                ApprovalInstance,
                null().label("approval_decision"),
            ).join(
                ApprovalInstance,
                ApprovalInstance.id == ApprovalTask.instance_id,
            )
        else:
            statement = (
                select(ApprovalTask, ApprovalInstance, ApprovalDecision)
                .join(
                    ApprovalInstance,
                    ApprovalInstance.id == ApprovalTask.instance_id,
                )
                .outerjoin(
                    ApprovalDecision,
                    ApprovalDecision.task_id == ApprovalTask.id,
                )
            )
        statement = statement.where(or_(*visibility_conditions)).order_by(
            ApprovalTask.activated_at.desc().nullslast(),
            ApprovalTask.id,
        )
        if status is not None:
            statement = statement.where(
                ApprovalTask.status == ApprovalTaskStatus(status)
            )
        if process_key is not None:
            statement = statement.where(
                ApprovalInstance.process_key == process_key
            )
        if activated_from is not None:
            statement = statement.where(
                ApprovalTask.activated_at >= activated_from
            )
        if activated_to is not None:
            statement = statement.where(
                ApprovalTask.activated_at <= activated_to
            )
        result = db.execute(
            statement.execution_options(yield_per=100, stream_results=True)
        )
        try:
            for task, instance, decision in result:
                yield ApprovalTaskCandidate(
                    task=task,
                    instance=instance,
                    decision=decision,
                    read_context=read_context,
                )
        finally:
            result.close()

    def summary(
        self,
        db: Session,
        *,
        actor: User,
        candidate: ApprovalTaskCandidate,
    ) -> ApprovalTaskSummary | None:
        task = candidate.task
        instance = candidate.instance
        decision = candidate.decision
        if not self._access.can_view(
            db,
            actor=actor,
            instance=instance,
            task=task,
            decision=decision,
            read_context=candidate.read_context,
        ):
            return None
        return ApprovalTaskSummary(
            task_id=task.id,
            instance_id=instance.id,
            process_key=instance.process_key,
            subject_type=instance.subject_type,
            step_key=task.step_key,
            step_label=task.step_label,
            status=task.status.value,
            submitted_at=instance.submitted_at,
            activated_at=task.activated_at,
            completed_at=task.completed_at,
            subject=self._registry.summary(
                instance.subject_type,
                db,
                instance,
            ),
        )


class ApprovalApiRuntime:
    """Bounded API read facade over the Task 8 domain-neutral runtime.

    Task 8 currently returns the actor-visible set as one sequence. This facade
    owns V1 projection/filter/pagination and refuses unexpectedly large scans;
    the HTTP router never loads or slices task collections.
    """

    def __init__(
        self,
        runtime: ApprovalRuntime,
        *,
        reader: ApprovalTaskReaderPort,
        max_scan: int = DEFAULT_APPROVAL_RAW_SCAN_LIMIT,
    ) -> None:
        self.core_runtime = runtime
        self.reader = reader
        self._max_scan = max_scan

    def list_tasks(
        self,
        db: Session,
        *,
        actor: User,
        status: str | None,
        process_key: str | None,
        activated_from: datetime | None,
        activated_to: datetime | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        normalized_from = self._utc_boundary(activated_from)
        normalized_to = self._utc_boundary(activated_to)
        if (
            normalized_from is not None
            and normalized_to is not None
            and normalized_from > normalized_to
        ):
            raise ToolError("approval_date_filter_invalid")
        visible = self._bounded_reader_summaries(
            db,
            actor=actor,
            status=status,
            process_key=process_key,
            activated_from=normalized_from,
            activated_to=normalized_to,
        )
        filtered = [
            item
            for item in visible
            if (process_key is None or item.process_key == process_key)
            and (
                normalized_from is None
                or (
                    item.activated_at is not None
                    and item.activated_at >= normalized_from
                )
            )
            and (
                normalized_to is None
                or (
                    item.activated_at is not None
                    and item.activated_at <= normalized_to
                )
            )
        ]
        return {
            "items": [item.model_dump(mode="json") for item in filtered[offset : offset + limit]],
            "offset": offset,
            "limit": limit,
            "total": len(filtered),
        }

    def _bounded_reader_summaries(
        self,
        db: Session,
        *,
        actor: User,
        status: str | None,
        process_key: str | None,
        activated_from: datetime | None,
        activated_to: datetime | None,
    ) -> list[ApprovalTaskSummary]:
        iterator = iter(
            self.reader.candidates(
                db,
                actor=actor,
                status=status,
                process_key=process_key,
                activated_from=activated_from,
                activated_to=activated_to,
            )
        )
        visible: list[ApprovalTaskSummary] = []
        try:
            for index, candidate in enumerate(iterator, start=1):
                if index > self._max_scan:
                    raise ToolError("approval_page_limit_exceeded")
                item = self.reader.summary(
                    db,
                    actor=actor,
                    candidate=candidate,
                )
                if item is not None:
                    visible.append(item)
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()
        return visible

    @staticmethod
    def _utc_boundary(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ToolError("approval_date_filter_invalid")
        return value.astimezone(timezone.utc)

    def get_task(
        self, db: Session, *, actor: User, task_id: uuid.UUID
    ) -> dict[str, Any]:
        return self.core_runtime.get_actor_task(
            db, actor=actor, task_id=task_id
        ).model_dump(mode="json")

    def approve(
        self,
        db: Session,
        *,
        actor: User,
        task_id: uuid.UUID,
        client_operation_id: uuid.UUID,
        comment: str | None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            self.core_runtime.approve_task(
                db,
                actor=actor,
                task_id=task_id,
                client_operation_id=client_operation_id,
                comment=comment,
                request_id=request_id,
                channel="manual",
            )
        )

    def reject(
        self,
        db: Session,
        *,
        actor: User,
        task_id: uuid.UUID,
        client_operation_id: uuid.UUID,
        reason: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            self.core_runtime.reject_task(
                db,
                actor=actor,
                task_id=task_id,
                client_operation_id=client_operation_id,
                reason=reason,
                request_id=request_id,
                channel="manual",
            )
        )

    @staticmethod
    def _transition(value) -> dict[str, Any]:
        return {
            "instance_id": value.instance.id,
            "status": value.instance.status.value,
            "current_step_key": value.instance.current_step_key,
            "replayed": value.replayed,
        }


__all__ = [
    "ApprovalApiRuntime",
    "ApprovalTaskReaderPort",
    "DEFAULT_APPROVAL_RAW_SCAN_LIMIT",
    "ProcurementRequestReaderPort",
    "ProcurementRuntime",
    "SqlAlchemyApprovalTaskReader",
    "SqlAlchemyProcurementRequestReader",
]
