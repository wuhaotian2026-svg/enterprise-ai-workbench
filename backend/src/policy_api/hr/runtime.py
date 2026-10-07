from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from time import perf_counter
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from policy_api.assistant_drafts.presentation import (
    ActionableClarificationProjection,
)
from policy_api.assistant_drafts.models import DraftStatus
from policy_api.assistant_drafts.store import AssistantDraftStore, DraftSnapshot
from policy_api.hr.draft import (
    HrDraftMerge,
    merge_hr_draft,
    project_hr_clarification,
)
from policy_api.hr.draft_activation import (
    HrDraftActivationPolicy,
    build_hr_draft_activation_policy,
)
from policy_api.hr.enums import LeaveRequestStatus
from policy_api.hr.models import (
    EmployeeProfile,
    HrConversation,
    HrTurn,
    LeaveRequest,
    LeaveType,
)
from policy_api.hr.repository import find_review_request, list_review_requests
from policy_api.hr.schemas import HrDomainError, LeaveRequestView
from policy_api.hr.slot_schema import HR_SLOT_SCHEMA
from policy_api.hr.slot_validation import validate_hr_candidates
from policy_api.hr.service import (
    approve_leave_request,
    cancel_leave_request,
    get_my_leave_balances,
    get_my_leave_request,
    list_my_leave_requests,
    reject_leave_request,
    submit_leave_request,
)
from policy_api.hr.tools import build_hr_tool_definitions, propose_hr_write
from policy_api.hr.tool_flow_policy import HrToolFlowPolicy, build_hr_tool_flow_policy
from policy_api.knowledge.tools import (
    PolicySearchOutcome,
    build_knowledge_tool_definition,
)
from policy_api.models import User
from policy_api.slot_extraction.controls import parse_draft_control, validate_current_user_turn_text
from policy_api.slot_extraction.errors import SlotExtractionError
from policy_api.slot_extraction.merge import CandidateValidationResult
from policy_api.slot_extraction.service import SlotExtractionService
from policy_api.slot_extraction.service import (
    PreparedExtraction,
    SlotExtractionPreparationRequest,
)
from policy_api.tools.audit import AuditSummary
from policy_api.tools.confirmation import (
    ToolExecutionResource,
    cancel_tool_confirmation,
    canonical_arguments_hash,
    confirm_tool_execution,
    create_tool_confirmation,
)
from policy_api.tools.definitions import ToolContext, ToolDefinition
from policy_api.tools.enums import ToolConfirmationStatus, ToolInvocationStatus
from policy_api.tools.executor import ToolExecutor
from policy_api.tools.errors import ToolError
from policy_api.tools.models import ToolConfirmation, ToolInvocation
from policy_api.tools.orchestrator import (
    SYSTEM_MESSAGE,
    BoundedToolOrchestrator,
    ToolLifecycleObservation,
)
from policy_api.tools.planner_client import ToolPlanningClient
from policy_api.tools.registry import ToolRegistry
from policy_api.tools.schemas import (
    AssistantDraftBlock,
    ClarificationBlock,
    ConfirmationBlock,
    ErrorBlock,
    TextBlock,
    ToolTurnResponse,
)
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityResolver,
    CapabilityScope,
)
from policy_api.workbench.events import (
    EventInput,
    ProductEventEmitter,
    ProductEventValidationError,
)


PolicySearch = Callable[[Session, str], PolicySearchOutcome]


def _map_hr_confirmation_failure(error: Exception) -> ToolError:
    if isinstance(error, ProductEventValidationError):
        return ToolError(
            "tool_execution_non_retryable",
            metadata={
                "failure_stage": "product_event",
                "internal_error_code": error.code,
            },
        )
    return ToolError("tool_execution_failed")


class _UnsetOrganizationUnit:
    pass


_UNSET_ORGANIZATION_UNIT = _UnsetOrganizationUnit()

_INTERNAL_PLANNING_PATTERN = re.compile(
    r"(?:\bI need to\b|\bLet me\b|\bI should\b|\bActually\b|"
    r"\bfact_flags\b|\bcollected_argument_names\b|\bvisible_tool_names\b|"
    r"\bTRUSTED_FLOW_CONTROL\b|\bready_to_propose\b|\brespond_only\b)",
    re.IGNORECASE,
)


def hr_text_postcondition(text: str) -> str:
    """Prevent provider planning/state dumps from becoming user-visible text."""
    canonical = text.strip()
    if not canonical:
        return "请补充或确认本次请假所需的信息。"
    if _INTERNAL_PLANNING_PATTERN.search(canonical):
        return "请补充或确认本次请假所需的信息。"
    return canonical


_HR_DRAFT_FIELD_LABELS = {
    "leave_type_code": "请假类型",
    "start_date": "开始日期",
    "end_date": "结束日期",
    "reason": "请假原因",
    "year": "请假年份",
    "date_range": "请假日期",
}
def _project_hr_clarification(
    merge: HrDraftMerge,
) -> ActionableClarificationProjection:
    return project_hr_clarification(
        missing_fields=merge.missing_fields,
        pending=merge.pending,
    )


def _with_hr_draft_guidance(
    response: ToolTurnResponse,
    merge: HrDraftMerge,
) -> ToolTurnResponse:
    """Make incomplete-draft guidance agree with the trusted draft snapshot."""
    projection = _project_hr_clarification(merge)
    if (
        not projection.clarification_fields
        or response.read_calls > 0
        or response.write_proposals > 0
        or any(block.type in {"error", "confirmation"} for block in response.blocks)
    ):
        return response
    if projection.missing_fields:
        labels = "、".join(
            _HR_DRAFT_FIELD_LABELS.get(name, "待补充信息")
            for name in projection.missing_fields
        )
        text = (
            f"已记录本会话中你明确提供的请假信息，请只补充：{labels}。"
            "无需重复已经提供的内容。"
        )
    else:
        labels = "、".join(
            _HR_DRAFT_FIELD_LABELS.get(name, "待确认信息")
            for name in projection.pending_fields
        )
        text = f"以下信息仍需确认：{labels}。已验证的其他内容会继续保留。"
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


def _refuse_policy_search(_db: Session, _query: str) -> PolicySearchOutcome:
    return PolicySearchOutcome(
        status="refused",
        text=None,
        refusal_reason="insufficient_evidence",
        citations=(),
    )


@dataclass(slots=True)
class HrRuntime:
    planner: ToolPlanningClient
    slot_extraction: SlotExtractionService | None = None
    capability_resolver: CapabilityResolver = field(
        default_factory=CapabilityResolver
    )
    search_policy: PolicySearch = _refuse_policy_search
    max_model_calls: int = 3
    max_read_calls: int = 4
    confirmation_ttl_seconds: int = 600
    product_event_emitter: ProductEventEmitter = field(default_factory=ProductEventEmitter)
    draft_store: AssistantDraftStore = field(default_factory=AssistantDraftStore)
    draft_activation_policy: HrDraftActivationPolicy = field(
        default_factory=build_hr_draft_activation_policy
    )
    _closed: bool = field(default=False, init=False, repr=False)

    def create_conversation(
        self, db: Session, actor_user_id: UUID, title: str | None
    ) -> dict[str, object]:
        conversation = HrConversation(
            owner_user_id=actor_user_id,
            title=(title or "").strip() or "新 HR 对话",
            is_archived=False,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return self._conversation_payload(conversation)

    def list_conversations(
        self, db: Session, actor_user_id: UUID
    ) -> list[dict[str, object]]:
        conversations = db.scalars(
            select(HrConversation)
            .where(
                HrConversation.owner_user_id == actor_user_id,
                HrConversation.is_archived.is_(False),
            )
            .order_by(HrConversation.updated_at.desc())
        ).all()
        return [self._conversation_payload(item) for item in conversations]

    def archive_conversation(
        self, db: Session, actor_user_id: UUID, conversation_id: UUID
    ) -> None:
        try:
            conversation = db.scalar(
                select(HrConversation)
                .where(
                    HrConversation.id == conversation_id,
                    HrConversation.owner_user_id == actor_user_id,
                    HrConversation.is_archived.is_(False),
                )
                .with_for_update()
            )
            if conversation is None:
                raise HrDomainError("hr_conversation_not_found")
            conversation.is_archived = True
            self.draft_store.transition(
                db,
                owner_user_id=actor_user_id,
                module_key="hr",
                conversation_id=conversation_id,
                status=DraftStatus.CLOSED,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise

    def get_conversation(
        self, db: Session, actor_user_id: UUID, conversation_id: UUID
    ) -> dict[str, object]:
        conversation = self._owned_conversation(
            db, actor_user_id, conversation_id
        )
        turns = db.scalars(
            select(HrTurn)
            .where(
                HrTurn.conversation_id == conversation.id,
                HrTurn.owner_user_id == actor_user_id,
            )
            .order_by(HrTurn.created_at.asc())
        ).all()
        turn_payloads = [self._turn_payload(item) for item in turns]
        return {
            **self._conversation_payload(conversation),
            "turns": self._project_confirmation_history(
                db,
                actor_user_id=actor_user_id,
                turns=turn_payloads,
            ),
        }

    def run_turn(
        self,
        db: Session,
        actor_user_id: UUID,
        conversation_id: UUID,
        client_turn_id: UUID,
        text: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        conversation = self._owned_conversation(
            db, actor_user_id, conversation_id
        )
        existing = db.scalar(
            select(HrTurn).where(
                HrTurn.owner_user_id == actor_user_id,
                HrTurn.client_turn_id == client_turn_id,
            )
        )
        if existing is not None:
            if existing.conversation_id != conversation_id or existing.content != text:
                raise HrDomainError("client_turn_id_conflict")
            if not isinstance(existing.blocks, dict) or not existing.blocks:
                response = self._slot_extraction_failure_response(
                    "turn_outcome_indeterminate",
                    slot_extraction_calls=0,
                )
                return {
                    "client_turn_id": client_turn_id,
                    "text": text,
                    **response.model_dump(mode="json"),
                }
            return self._stored_turn_response(existing)

        actor = db.get(User, actor_user_id)
        if actor is None or not actor.is_active:
            raise HrDomainError("authentication_required")
        try:
            validate_current_user_turn_text(text)
        except SlotExtractionError:
            raise HrDomainError("request_validation_failed") from None
        turn = HrTurn(
            id=uuid4(),
            conversation_id=conversation.id,
            owner_user_id=actor_user_id,
            client_turn_id=client_turn_id,
            role="assistant",
            content=text,
            blocks={},  # type: ignore[arg-type]
            model_name=getattr(self.planner, "model", None),
        )
        db.add(turn)
        db.flush()

        def persist_response(response: ToolTurnResponse, *, started: float) -> dict[str, object]:
            payload = response.model_dump(mode="json")
            turn.blocks = payload  # type: ignore[assignment]
            turn.latency_ms = max(0, round((perf_counter() - started) * 1000))
            conversation.updated_at = datetime.now(timezone.utc)
            db.commit()
            return {"client_turn_id": client_turn_id, "text": text, **payload}

        started = perf_counter()
        preexisting_draft = self.draft_store.get_active(
            db,
            owner_user_id=actor_user_id,
            module_key="hr",
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
            field_labels=_HR_DRAFT_FIELD_LABELS,
        )
        prepared: PreparedExtraction | None = None
        validation = CandidateValidationResult(accepted={}, pending={}, rejected=())
        slot_extraction_calls = 0
        if control is None:
            if self.slot_extraction is None:
                response = self._slot_extraction_failure_response(
                    "slot_extraction_not_configured",
                    slot_extraction_calls=0,
                )
                return persist_response(response, started=started)
            try:
                prepared = self.slot_extraction.prepare(
                    db,
                    SlotExtractionPreparationRequest(
                        owner_user_id=actor_user_id,
                        module_key="hr",
                        conversation_id=conversation_id,
                        client_turn_id=client_turn_id,
                        current_user_turn_text=text,
                        slot_schema=HR_SLOT_SCHEMA,
                    ),
                    lambda current_text, envelope: validate_hr_candidates(
                        text=current_text,
                        envelope=envelope,
                        today=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
                        source_turn_id=str(turn.id),
                    ),
                )
            except SlotExtractionError as exc:
                slot_extraction_calls = exc.slot_extraction_calls
                response = self._slot_extraction_failure_response(
                    exc.code,
                    slot_extraction_calls=slot_extraction_calls,
                    retryable=exc.retryable,
                )
                return persist_response(response, started=started)
            validation = prepared.dispositions
            slot_extraction_calls = prepared.slot_extraction_calls

        active_draft = self.draft_store.get_active(
            db,
            owner_user_id=actor_user_id,
            module_key="hr",
            conversation_id=conversation_id,
            for_update=True,
        )
        try:
            draft_merge = merge_hr_draft(
                fields=active_draft.fields if active_draft else {},
                pending=active_draft.pending if active_draft else {},
                sources=active_draft.sources if active_draft else {},
                validation=validation,
                control=control,
                source_turn_id=str(turn.id),
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
            response = self._slot_extraction_failure_response(
                exc.code,
                slot_extraction_calls=slot_extraction_calls,
                retryable=exc.retryable,
            )
            return persist_response(response, started=started)

        if draft_merge.clear_requested:
            self.draft_store.transition(
                db,
                owner_user_id=actor_user_id,
                module_key="hr",
                conversation_id=conversation_id,
                status=DraftStatus.CLEARED,
            )
            response = ToolTurnResponse(
                blocks=(TextBlock(text="已清空当前 HR 办事草稿。"),),
                model_calls=0,
                read_calls=0,
                write_proposals=0,
                slot_extraction_calls=slot_extraction_calls,
            )
            return persist_response(response, started=started)

        draft_snapshot: DraftSnapshot | None = active_draft
        current_intent = HrToolFlowPolicy._classify_intent(text)
        if prepared is not None and self.slot_extraction is not None:
            try:
                self.slot_extraction.complete_success(db, prepared)
            except SlotExtractionError as exc:
                # Repository convergence is part of the current valid transaction.
                # Commit it before persisting the stable no-merge response.
                db.commit()
                response = self._slot_extraction_failure_response(
                    exc.code,
                    slot_extraction_calls=prepared.slot_extraction_calls,
                    retryable=exc.retryable,
                )
                return persist_response(response, started=started)
        activation = self.draft_activation_policy.decide(
            current_intent=current_intent,
            has_active_draft=active_draft is not None,
            validation=validation,
        )
        if activation.should_save:
            draft_snapshot = self.draft_store.save(
                db,
                owner_user_id=actor_user_id,
                module_key="hr",
                conversation_id=conversation_id,
                intent="submit_leave",
                fields=draft_merge.fields,
                sources=draft_merge.sources,
                pending=draft_merge.pending,
            )

        if draft_snapshot is not None and (
            draft_merge.missing_fields
            or draft_merge.pending
            or validation.rejected_count > 0
        ):
            response = self._hr_draft_clarification_response(
                draft_snapshot,
                draft_merge,
                validation,
                slot_extraction_calls=slot_extraction_calls,
            )
            return persist_response(response, started=started)

        # Commit the validated draft and extraction outcome before any Planner call.
        db.commit()
        organization_unit_id = db.scalar(select(EmployeeProfile.organization_unit_id).where(
            EmployeeProfile.user_id == actor_user_id,
            EmployeeProfile.is_active.is_(True)))
        event_sequence = 0

        def append_event(event_name: str, *, outcome: str,
                dimensions: dict[str, object], duration_ms: int | None = None) -> None:
            nonlocal event_sequence
            event_sequence += 1
            self.product_event_emitter.append(db, EventInput(
                event_id=uuid5(NAMESPACE_URL,
                    f"policy-assistant:hr-turn:{client_turn_id}:{event_sequence}:{event_name}"),
                event_name=event_name, module_key="hr-assistant",
                actor_user_id=actor.id, organization_unit_id=organization_unit_id,
                role_snapshot=actor.role.value, request_id=request_id, outcome=outcome,
                duration_ms=duration_ms, dimensions=dimensions))

        append_event("hr_turn_submitted", outcome="submitted",
            dimensions={"message_length_bucket": self._message_length_bucket(len(text))})
        intent_recorded = False

        def observe(item: ToolLifecycleObservation) -> None:
            nonlocal intent_recorded
            if item.kind == "tool_planned" and item.tool_name is not None:
                if not intent_recorded:
                    append_event("hr_intent_resolved", outcome="resolved", dimensions={
                        "intent": self._intent_for_tool(item.tool_name),
                        "clarification_required": False,
                    })
                    intent_recorded = True
                append_event("tool_planned", outcome="planned", dimensions={
                    "tool_name": item.tool_name, "risk_level": item.risk_level,
                })
            elif item.kind == "tool_validation_failed" and item.tool_name is not None:
                append_event("tool_validation_failed", outcome="failed", dimensions={
                    "tool_name": item.tool_name, "error_code": item.error_code,
                    "retryable": item.retryable,
                })
            elif item.kind == "tool_read_succeeded" and item.tool_name is not None:
                append_event("tool_read_succeeded", outcome="succeeded",
                    duration_ms=item.duration_ms, dimensions={
                        "tool_name": item.tool_name,
                        "processing_time_bucket": self._processing_time_bucket(item.duration_ms or 0),
                    })
            elif item.kind == "flow_error" and item.error_code is not None:
                append_event("hr_flow_error", outcome="failed", dimensions={
                    "error_code": item.error_code, "retryable": item.retryable,
                })

        response = self._orchestrator(
            db,
            turn,
            observer=observe,
            draft_snapshot=draft_snapshot,
        ).run(
            text,
            ToolContext(actor_user_id=actor_user_id, role=actor.role),
        )
        response = self._with_slot_extraction_calls(
            response,
            slot_extraction_calls,
        )
        for block in response.blocks:
            if isinstance(block, ConfirmationBlock):
                append_event(
                    "confirmation_shown",
                    outcome="shown",
                    dimensions={
                        "tool_name": block.tool_name,
                        "risk_level": "write",
                    },
                )
        if draft_snapshot is not None:
            if any(isinstance(block, ConfirmationBlock) for block in response.blocks):
                self.draft_store.transition(
                    db,
                    owner_user_id=actor_user_id,
                    module_key="hr",
                    conversation_id=conversation_id,
                    status=DraftStatus.CLOSED,
                )
            else:
                response = _with_hr_draft_guidance(response, draft_merge)
                response = self._with_draft_block(
                    response,
                    draft_snapshot,
                    draft_merge,
                    module_key="hr",
                )
        if not intent_recorded and not any(
            item.type == "error" for item in response.blocks
        ):
            append_event("hr_intent_resolved", outcome="resolved", dimensions={
                "intent": "unknown", "clarification_required": False,
            })
        return persist_response(response, started=started)

    def leave_balances(
        self, db: Session, actor_user_id: UUID, year: int | None
    ) -> list[dict[str, object]]:
        return [
            {
                "account_id": item.account_id,
                "leave_type_code": item.leave_type_code.value,
                "leave_type_name": item.leave_type_name,
                "year": item.year,
                "entitled": str(item.entitled),
                "used": str(item.used),
                "reserved": str(item.reserved),
                "available": str(item.available),
            }
            for item in get_my_leave_balances(db, actor_user_id, year=year)
        ]

    def list_leave_requests(
        self, db: Session, actor_user_id: UUID, status: str | None
    ) -> list[dict[str, object]]:
        values = list_my_leave_requests(db, actor_user_id)
        if status is not None:
            expected = LeaveRequestStatus(status)
            values = tuple(item for item in values if item.status == expected)
        return [self._request_view_payload(item) for item in values]

    def get_leave_request(
        self, db: Session, actor_user_id: UUID, request_id: UUID
    ) -> dict[str, object]:
        return self._request_view_payload(
            get_my_leave_request(db, actor_user_id, request_id)
        )

    def review_queue(
        self,
        db: Session,
        reviewer_user_id: UUID,
        status: str,
    ) -> list[dict[str, object]]:
        expected = LeaveRequestStatus(status)
        _reviewer, scope = self._review_scope(db, reviewer_user_id)
        rows = list_review_requests(db, status=expected, scope=scope)
        return [
            self._request_model_payload(request, leave_type, employee)
            for request, leave_type, employee in rows
        ]

    def review_detail(
        self,
        db: Session,
        reviewer_user_id: UUID,
        request_id: UUID,
    ) -> dict[str, object]:
        _reviewer, scope = self._review_scope(
            db,
            reviewer_user_id,
            missing_code="leave_request_not_found",
        )
        row = find_review_request(db, request_id=request_id, scope=scope)
        if row is None:
            raise HrDomainError("leave_request_not_found")
        return self._request_model_payload(row[0], row[1], row[2])

    def approve(
        self,
        db: Session,
        reviewer_user_id: UUID,
        request_id: UUID,
        client_operation_id: UUID,
        *,
        trace_id: str | None = None,
    ) -> dict[str, object]:
        _reviewer, scope = self._review_scope(
            db,
            reviewer_user_id,
            missing_code="leave_request_not_found",
        )
        return self._request_view_payload(
            approve_leave_request(
                db,
                reviewer_user_id,
                request_id,
                client_operation_id,
                review_scope=scope,
                capability_resolver=self.capability_resolver,
                after_mutation=lambda item: self._append_review_event(
                    db, reviewer_user_id, item, client_operation_id,
                    decision="approved", trace_id=trace_id),
            )
        )

    def reject(
        self,
        db: Session,
        reviewer_user_id: UUID,
        request_id: UUID,
        client_operation_id: UUID,
        reason: str,
        *,
        trace_id: str | None = None,
    ) -> dict[str, object]:
        _reviewer, scope = self._review_scope(
            db,
            reviewer_user_id,
            missing_code="leave_request_not_found",
        )
        return self._request_view_payload(
            reject_leave_request(
                db,
                reviewer_user_id,
                request_id,
                client_operation_id,
                reason,
                review_scope=scope,
                capability_resolver=self.capability_resolver,
                after_mutation=lambda item: self._append_review_event(
                    db, reviewer_user_id, item, client_operation_id,
                    decision="rejected", trace_id=trace_id),
            )
        )

    def _review_scope(
        self,
        db: Session,
        reviewer_user_id: UUID,
        *,
        missing_code: str = "capability_required",
    ) -> tuple[User, CapabilityScope]:
        reviewer = db.get(User, reviewer_user_id)
        if reviewer is None:
            raise HrDomainError(missing_code)
        scope = self.capability_resolver.scope_for(
            db,
            reviewer,
            Capability.HR_LEAVE_REVIEW,
        )
        if scope is None:
            raise HrDomainError(missing_code)
        return reviewer, scope

    def cancel_intent(
        self,
        db: Session,
        actor_user_id: UUID,
        request_id: UUID,
        client_operation_id: UUID,
        *,
        trace_id: str | None = None,
    ) -> dict[str, object]:
        provider_call_id = f"cancel-intent-{client_operation_id}"
        existing_invocation = db.scalar(
            select(ToolInvocation).where(
                ToolInvocation.actor_user_id == actor_user_id,
                ToolInvocation.provider_call_id == provider_call_id,
                ToolInvocation.tool_name == "hr.cancel_leave_request",
            )
        )
        if existing_invocation is not None:
            confirmation = db.scalar(
                select(ToolConfirmation).where(
                    ToolConfirmation.invocation_id == existing_invocation.id
                )
            )
            if confirmation is None:
                raise HrDomainError("confirmation_not_found")
            return self._confirmation_payload(confirmation)

        request = get_my_leave_request(db, actor_user_id, request_id)
        if request.status != LeaveRequestStatus.PENDING:
            raise HrDomainError("leave_request_state_conflict")
        normalized = {"request_id": str(request_id)}
        request_payload = self._request_view_payload(request)
        preview = jsonable_encoder(
            {
                key: request_payload[key]
                for key in (
                    "request_number",
                    "leave_type_code",
                    "leave_type_name",
                    "start_date",
                    "end_date",
                    "workday_count",
                    "reason",
                    "status",
                )
            }
        )
        invocation = ToolInvocation(
            conversation_id=request_id,
            turn_id=client_operation_id,
            actor_user_id=actor_user_id,
            provider_call_id=provider_call_id,
            tool_name="hr.cancel_leave_request",
            provider_tool_name="hr_cancel_leave_request",
            risk_level="write",
            status=ToolInvocationStatus.PROPOSED,
            arguments_hash=canonical_arguments_hash(normalized),
        )
        db.add(invocation)
        db.flush()
        confirmation = create_tool_confirmation(
            db,
            invocation=invocation,
            owner_user_id=actor_user_id,
            normalized_arguments=normalized,
            preview=preview,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=self.confirmation_ttl_seconds),
            audit_summary=AuditSummary.for_execution(
                tool_name=invocation.tool_name,
                risk_level=invocation.risk_level,
                outcome="confirmation_pending",
            ),
        )
        self._append_actor_event(
            db, actor_user_id, event_name="confirmation_shown",
            event_key=confirmation.id, trace_id=trace_id, outcome="shown",
            dimensions={"tool_name": confirmation.tool_name, "risk_level": "write"},
        )
        db.commit()
        return self._confirmation_payload(confirmation)

    def cancel_confirmation(
        self, db: Session, actor_user_id: UUID, confirmation_id: UUID, *,
        trace_id: str | None = None,
    ) -> dict[str, object]:
        previous = db.get(ToolConfirmation, confirmation_id)
        was_cancelled = (
            previous is not None
            and previous.status.value == "cancelled"
        )
        was_expired = (
            previous is not None
            and previous.status.value == "expired"
        )
        try:
            confirmation = cancel_tool_confirmation(
                db, actor_user_id, confirmation_id
            )
            if not was_cancelled:
                self._append_actor_event(
                    db, actor_user_id, event_name="confirmation_cancelled",
                    event_key=confirmation.id, trace_id=trace_id, outcome="cancelled",
                    dimensions={"tool_name": confirmation.tool_name},
                )
            db.commit()
        except ToolError as exc:
            if exc.code == "confirmation_expired" and not was_expired:
                expired = db.get(ToolConfirmation, confirmation_id)
                if expired is not None and expired.owner_user_id == actor_user_id:
                    try:
                        self._append_actor_event(
                            db, actor_user_id,
                            event_name="confirmation_expired",
                            event_key=expired.id, trace_id=trace_id,
                            outcome="expired",
                            dimensions={"tool_name": expired.tool_name},
                        )
                        db.commit()
                    except Exception:
                        db.rollback()
                        raise
            else:
                db.rollback()
            raise
        except Exception:
            db.rollback()
            raise
        return {
            "confirmation_id": confirmation.id,
            "status": confirmation.status.value,
        }

    def confirm(
        self,
        db: Session,
        actor_user_id: UUID,
        confirmation_id: UUID,
        client_operation_id: UUID,
        *,
        trace_id: str | None = None,
    ) -> dict[str, object]:
        confirmation = db.scalar(
            select(ToolConfirmation).where(
                ToolConfirmation.id == confirmation_id,
                ToolConfirmation.owner_user_id == actor_user_id,
            )
        )
        if confirmation is None:
            raise ToolError("confirmation_not_found")
        normalized_arguments = dict(confirmation.normalized_arguments)
        executed_request: LeaveRequestView | None = None

        def execute(
            session: Session, pending: ToolConfirmation
        ) -> ToolExecutionResource:
            nonlocal executed_request
            if pending.tool_name == "hr.submit_leave_request":
                executed_request = submit_leave_request(
                    session,
                    actor_user_id,
                    pending.id,
                    client_operation_id,
                    commit=False,
                )
            elif pending.tool_name == "hr.cancel_leave_request":
                try:
                    leave_request_id = UUID(
                        str(pending.normalized_arguments["request_id"])
                    )
                except (KeyError, TypeError, ValueError):
                    raise ToolError("confirmation_arguments_mismatch") from None
                executed_request = cancel_leave_request(
                    session,
                    actor_user_id,
                    leave_request_id,
                    pending.id,
                    client_operation_id,
                    commit=False,
                )
            else:
                raise ToolError("unknown_tool")
            return ToolExecutionResource(
                resource_type="leave_request",
                resource_id=executed_request.id,
                result_summary={
                    "status": executed_request.status.value,
                    "request_number": executed_request.request_number,
                    "outcome": "succeeded",
                },
            )

        def append_success_events(
            session: Session,
            pending: ToolConfirmation,
            _resource: ToolExecutionResource,
        ) -> None:
            if executed_request is None:
                raise RuntimeError("tool_execution_resource_missing")
            self._append_actor_event(
                session,
                actor_user_id,
                event_name="confirmation_confirmed",
                event_key=pending.id,
                discriminator=client_operation_id,
                trace_id=trace_id,
                outcome="confirmed",
                dimensions={"tool_name": pending.tool_name},
            )
            self._append_leave_event(
                session,
                actor_user_id,
                executed_request,
                client_operation_id,
                event_name=(
                    "leave_request_submitted"
                    if pending.tool_name == "hr.submit_leave_request"
                    else "leave_request_cancelled"
                ),
                trace_id=trace_id,
            )

        def append_expired_event(
            session: Session, expired: ToolConfirmation
        ) -> None:
            self._append_actor_event(
                session,
                actor_user_id,
                event_name="confirmation_expired",
                event_key=expired.id,
                trace_id=trace_id,
                outcome="expired",
                dimensions={"tool_name": expired.tool_name},
            )

        resource = confirm_tool_execution(
            db,
            actor_user_id=actor_user_id,
            confirmation_id=confirmation_id,
            client_operation_id=client_operation_id,
            normalized_arguments=normalized_arguments,
            execute=execute,
            on_expired=append_expired_event,
            before_commit=append_success_events,
            failure_mapper=_map_hr_confirmation_failure,
        )
        request = executed_request or get_my_leave_request(
            db, actor_user_id, resource.resource_id
        )
        return {
            "type": "execution_result",
            "resource_type": "leave_request",
            "resource_id": request.id,
            "result": self._request_view_payload(request),
        }

    def _append_actor_event(
        self, db: Session, actor_user_id: UUID, *, event_name: str,
        event_key: UUID, trace_id: str | None, outcome: str,
        dimensions: dict[str, object], duration_ms: int | None = None,
        discriminator: UUID | None = None,
        organization_unit_id: UUID | None | _UnsetOrganizationUnit = (
            _UNSET_ORGANIZATION_UNIT
        ),
    ) -> None:
        actor = db.get(User, actor_user_id)
        if actor is None:
            raise HrDomainError("authentication_required")
        if isinstance(organization_unit_id, _UnsetOrganizationUnit):
            resolved_organization_unit_id = db.scalar(
                select(EmployeeProfile.organization_unit_id).where(
                    EmployeeProfile.user_id == actor_user_id,
                    EmployeeProfile.is_active.is_(True),
                )
            )
        else:
            resolved_organization_unit_id = organization_unit_id
        self.product_event_emitter.append(db, EventInput(
            event_id=uuid5(NAMESPACE_URL,
                f"policy-assistant:hr:{event_name}:{event_key}:{discriminator or ''}"),
            event_name=event_name, module_key="hr-assistant",
            actor_user_id=actor.id,
            organization_unit_id=resolved_organization_unit_id,
            role_snapshot=actor.role.value, request_id=trace_id, outcome=outcome,
            duration_ms=duration_ms, dimensions=dimensions))

    def _append_review_event(
        self, db: Session, reviewer_user_id: UUID, request: LeaveRequest,
        operation_id: UUID, *, decision: str, trace_id: str | None,
    ) -> None:
        organization_unit_id = db.scalar(select(EmployeeProfile.organization_unit_id).where(
            EmployeeProfile.id == request.employee_id))
        elapsed = request.reviewed_at - request.submitted_at if request.reviewed_at else None
        bucket = "same_day" if elapsed is None or elapsed.days == 0 else "later"
        self._append_actor_event(
            db, reviewer_user_id, event_name="leave_request_reviewed",
            event_key=request.id, discriminator=operation_id, trace_id=trace_id,
            outcome=decision, organization_unit_id=organization_unit_id,
            dimensions={"decision": decision, "processing_time_bucket": bucket},
        )

    def _append_leave_event(
        self, db: Session, actor_user_id: UUID, request: LeaveRequestView,
        operation_id: UUID, *, event_name: str, trace_id: str | None,
    ) -> None:
        organization_unit_id = db.scalar(
            select(EmployeeProfile.organization_unit_id)
            .join(LeaveRequest, LeaveRequest.employee_id == EmployeeProfile.id)
            .where(LeaveRequest.id == request.id)
        )
        if event_name == "leave_request_submitted":
            workdays = request.workday_count
            if workdays <= 1: workday_bucket = "0_1"
            elif workdays <= 2: workday_bucket = "1_2"
            elif workdays <= 5: workday_bucket = "3_5"
            else: workday_bucket = "6_plus"
            dimensions: dict[str, object] = {
                "leave_type": request.leave_type_code.value,
                "workday_count_bucket": workday_bucket,
            }
            outcome = "submitted"
        else:
            dimensions = {"leave_type": request.leave_type_code.value}
            outcome = "cancelled"
        self._append_actor_event(
            db, actor_user_id, event_name=event_name, event_key=request.id,
            discriminator=operation_id, trace_id=trace_id, outcome=outcome,
            organization_unit_id=organization_unit_id, dimensions=dimensions,
        )

    def _orchestrator(
        self,
        db: Session,
        turn: HrTurn,
        *,
        observer=None,
        draft_snapshot: DraftSnapshot | None = None,
    ) -> BoundedToolOrchestrator:
        definitions = (
            build_knowledge_tool_definition(
                lambda query: self.search_policy(db, query)
            ),
            *build_hr_tool_definitions(db),
        )
        registry = ToolRegistry(definitions)

        def persist(
            tool_name: str,
            normalized: dict[str, object],
            preview: dict[str, object],
            context: ToolContext,
        ) -> ConfirmationBlock:
            definition = registry.get(tool_name)
            invocation = ToolInvocation(
                conversation_id=turn.conversation_id,
                turn_id=turn.id,
                actor_user_id=context.actor_user_id,
                provider_call_id=f"runtime-{uuid4()}",
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
                    reason=(
                        str(normalized.get("reason"))
                        if normalized.get("reason") is not None
                        else None
                    ),
                ),
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
            return propose_hr_write(
                db,
                definition,
                arguments,
                context,
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
            observer=observer,
            flow_policy=build_hr_tool_flow_policy(
                draft_fields=draft_snapshot.fields if draft_snapshot else None,
                draft_intent=draft_snapshot.intent if draft_snapshot else None,
            ),
            system_message=(
                "你是企业 HR 办事助手。只能输出简洁、面向员工的中文最终回复；"
                "不得输出分析过程、内部规划、英文思考、flow-control、fact_flags 或工具 schema。 "
                + SYSTEM_MESSAGE
            ),
            text_postcondition=hr_text_postcondition,
        )

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
    def _hr_draft_clarification_response(
        snapshot: DraftSnapshot,
        merge: HrDraftMerge,
        validation: CandidateValidationResult,
        *,
        slot_extraction_calls: int,
    ) -> ToolTurnResponse:
        projection = _project_hr_clarification(merge)
        if projection.missing_fields:
            labels = "、".join(
                _HR_DRAFT_FIELD_LABELS.get(name, "待补充信息")
                for name in projection.missing_fields
            )
            text = (
                f"已记录本会话中你明确提供的请假信息，请只补充：{labels}。"
                "无需重复已经提供的内容。"
            )
        elif projection.pending_fields:
            labels = "、".join(
                _HR_DRAFT_FIELD_LABELS.get(name, "待确认信息")
                for name in projection.pending_fields
            )
            text = f"以下信息仍需确认：{labels}。已验证的其他内容会继续保留。"
        else:
            text = "本轮部分信息未通过校验，请按提示重新提供对应字段。"
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
        return HrRuntime._with_draft_block(
            response,
            snapshot,
            merge,
            module_key="hr",
        )

    @staticmethod
    def _with_draft_block(
        response: ToolTurnResponse,
        snapshot: DraftSnapshot,
        merge: HrDraftMerge,
        *,
        module_key: str,
    ) -> ToolTurnResponse:
        projection = _project_hr_clarification(merge)
        block = AssistantDraftBlock(
            module_key=module_key,  # type: ignore[arg-type]
            intent=snapshot.intent,
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
    def _message_length_bucket(length: int) -> str:
        if length <= 50: return "0_50"
        if length <= 200: return "51_200"
        if length <= 500: return "201_500"
        return "501_plus"

    @staticmethod
    def _processing_time_bucket(duration_ms: int) -> str:
        if duration_ms < 1_000: return "lt_1s"
        if duration_ms < 3_000: return "1s_3s"
        if duration_ms < 10_000: return "3s_10s"
        return "gte_10s"

    @staticmethod
    def _intent_for_tool(tool_name: str) -> str:
        if tool_name == "knowledge.search_policy":
            return "get_leave_policy"
        if tool_name.startswith("hr."):
            return tool_name.removeprefix("hr.")
        return "unknown"

    @staticmethod
    def _conversation_payload(conversation: HrConversation) -> dict[str, object]:
        return {
            "id": conversation.id,
            "title": conversation.title,
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at,
        }

    @staticmethod
    def _turn_payload(turn: HrTurn) -> dict[str, object]:
        return {
            **HrRuntime._stored_turn_response(turn),
            "created_at": turn.created_at,
        }

    def _project_confirmation_history(
        self,
        db: Session,
        *,
        actor_user_id: UUID,
        turns: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        confirmation_ids: set[UUID] = set()
        for turn in turns:
            blocks = turn.get("blocks")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, Mapping) or block.get("type") != "confirmation":
                    continue
                try:
                    confirmation_ids.add(UUID(str(block.get("confirmation_id"))))
                except (TypeError, ValueError):
                    continue
        if not confirmation_ids:
            return turns

        confirmations = {
            item.id: item
            for item in db.scalars(
                select(ToolConfirmation).where(
                    ToolConfirmation.id.in_(confirmation_ids),
                    ToolConfirmation.owner_user_id == actor_user_id,
                )
            )
        }
        resource_ids = {
            item.result_resource_id
            for item in confirmations.values()
            if item.status == ToolConfirmationStatus.CONSUMED
            and item.result_resource_type == "leave_request"
            and item.result_resource_id is not None
        }
        request_payloads: dict[UUID, dict[str, object]] = {}
        if resource_ids:
            rows = db.execute(
                select(LeaveRequest, LeaveType)
                .join(
                    EmployeeProfile,
                    EmployeeProfile.id == LeaveRequest.employee_id,
                )
                .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
                .where(
                    EmployeeProfile.user_id == actor_user_id,
                    LeaveRequest.id.in_(resource_ids),
                )
            ).tuples()
            request_payloads = {
                request.id: self._request_view_payload(
                    LeaveRequestView(
                        id=request.id,
                        request_number=request.request_number,
                        leave_type_code=leave_type.code,
                        leave_type_name=leave_type.display_name,
                        start_date=request.start_date,
                        end_date=request.end_date,
                        workday_count=request.workday_count,
                        reason=request.reason,
                        status=request.status,
                        submitted_at=request.submitted_at,
                        reviewed_at=request.reviewed_at,
                        rejection_reason=request.rejection_reason,
                        cancelled_at=request.cancelled_at,
                    )
                )
                for request, leave_type in rows
            }

        now = datetime.now(timezone.utc)
        projected_turns: list[dict[str, object]] = []
        for turn in turns:
            blocks = turn.get("blocks")
            if not isinstance(blocks, list):
                projected_turns.append(turn)
                continue
            projected_blocks: list[object] = []
            for block in blocks:
                if not isinstance(block, Mapping) or block.get("type") != "confirmation":
                    projected_blocks.append(block)
                    continue
                try:
                    confirmation_id = UUID(str(block.get("confirmation_id")))
                except (TypeError, ValueError):
                    projected_blocks.append(block)
                    continue
                confirmation = confirmations.get(confirmation_id)
                if confirmation is None:
                    projected_blocks.append(block)
                    continue
                if confirmation.status == ToolConfirmationStatus.PENDING:
                    if confirmation.expires_at > now:
                        projected_blocks.append(block)
                    else:
                        projected_blocks.append(
                            self._confirmation_terminal_payload(
                                confirmation,
                                status="expired",
                            )
                        )
                    continue
                if confirmation.status in {
                    ToolConfirmationStatus.CANCELLED,
                    ToolConfirmationStatus.EXPIRED,
                }:
                    projected_blocks.append(
                        self._confirmation_terminal_payload(
                            confirmation,
                            status=confirmation.status.value,
                        )
                    )
                    continue
                if (
                    confirmation.status == ToolConfirmationStatus.CONSUMED
                    and confirmation.result_resource_type == "leave_request"
                    and confirmation.result_resource_id in request_payloads
                ):
                    projected_blocks.append(
                        {
                            "type": "execution_result",
                            "resource_type": "leave_request",
                            "resource_id": confirmation.result_resource_id,
                            "result": request_payloads[confirmation.result_resource_id],
                        }
                    )
                    continue
                projected_blocks.append(
                    self._confirmation_terminal_payload(
                        confirmation,
                        status="executed",
                    )
                )
            projected_turns.append({**turn, "blocks": projected_blocks})
        return projected_turns

    @staticmethod
    def _confirmation_terminal_payload(
        confirmation: ToolConfirmation,
        *,
        status: str,
    ) -> dict[str, object]:
        return {
            "type": "confirmation_terminal",
            "confirmation_id": confirmation.id,
            "tool_name": confirmation.tool_name,
            "status": status,
        }

    @staticmethod
    def _stored_turn_response(turn: HrTurn) -> dict[str, object]:
        stored: Any = turn.blocks
        if not isinstance(stored, dict):
            raise HrDomainError("hr_turn_snapshot_invalid")
        return {
            "client_turn_id": turn.client_turn_id,
            "text": turn.content,
            **stored,
        }

    @staticmethod
    def _request_view_payload(item: LeaveRequestView) -> dict[str, object]:
        return {
            "id": item.id,
            "request_number": item.request_number,
            "leave_type_code": item.leave_type_code.value,
            "leave_type_name": item.leave_type_name,
            "start_date": item.start_date,
            "end_date": item.end_date,
            "workday_count": str(item.workday_count),
            "reason": item.reason,
            "status": item.status.value,
            "submitted_at": item.submitted_at,
            "reviewed_at": item.reviewed_at,
            "rejection_reason": item.rejection_reason,
            "cancelled_at": item.cancelled_at,
        }

    @staticmethod
    def _request_model_payload(
        request: LeaveRequest,
        leave_type: LeaveType,
        employee: EmployeeProfile,
    ) -> dict[str, object]:
        return {
            "id": request.id,
            "request_number": request.request_number,
            "leave_type_code": leave_type.code.value,
            "leave_type_name": leave_type.display_name,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "workday_count": str(request.workday_count),
            "reason": request.reason,
            "status": request.status.value,
            "submitted_at": request.submitted_at,
            "reviewer_user_id": request.reviewer_user_id,
            "reviewed_at": request.reviewed_at,
            "rejection_reason": request.rejection_reason,
            "cancelled_at": request.cancelled_at,
            "employee_number": employee.employee_number,
            "employee_display_name": employee.display_name,
        }

    @staticmethod
    def _confirmation_payload(
        confirmation: ToolConfirmation,
    ) -> dict[str, object]:
        return {
            "type": "confirmation",
            "confirmation_id": confirmation.id,
            "tool_name": confirmation.tool_name,
            "preview": dict(confirmation.preview),
            "expires_at": confirmation.expires_at,
        }

    @staticmethod
    def _owned_conversation(
        db: Session, actor_user_id: UUID, conversation_id: UUID
    ) -> HrConversation:
        conversation = db.scalar(
            select(HrConversation).where(
                HrConversation.id == conversation_id,
                HrConversation.owner_user_id == actor_user_id,
                HrConversation.is_archived.is_(False),
            )
        )
        if conversation is None:
            raise HrDomainError("hr_conversation_not_found")
        return conversation

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.planner.close()
