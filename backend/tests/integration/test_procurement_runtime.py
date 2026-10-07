from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from policy_api.approvals.enums import (
    ApprovalInstanceStatus,
    ApprovalTaskStatus,
    AssignmentKind,
)
from policy_api.approvals.models import ApprovalDecision, ApprovalInstance, ApprovalTask
from policy_api.approvals.runtime import ApprovalRuntime
from policy_api.approvals.schemas import SubjectSummary
from policy_api.approvals.subject_adapter import SubjectAdapterRegistry
from policy_api.knowledge.tools import PolicySearchOutcome
from policy_api.procurement.models import (
    AssistantConversation,
    AssistantTurn,
    ProcurementCommandOperation,
    ProcurementRequest,
    ProcurementRequestItem,
)
from policy_api.procurement.observability import ProcurementObservability
from policy_api.procurement.repository import ProcurementRepository
from policy_api.procurement.runtime import (
    ApprovalApiRuntime,
    ProcurementRuntime,
    SqlAlchemyApprovalTaskReader,
    procurement_text_postcondition,
)
from policy_api.procurement.schemas import ProcurementRequestInput
from policy_api.procurement.slot_schema import PROCUREMENT_SLOT_SCHEMA
from policy_api.procurement.service import (
    PROCUREMENT_REQUEST_V1,
    SUBJECT_TYPE,
    ProcurementApprovalAccess,
    ProcurementSubjectAdapter,
)
from policy_api.tools.audit import AuditSummary
from policy_api.tools.confirmation import canonical_arguments_hash, create_tool_confirmation
from policy_api.tools.enums import ToolConfirmationStatus, ToolInvocationStatus
from policy_api.tools.errors import ToolError
from policy_api.tools.definitions import ToolContext
from policy_api.tools.types import PlannedToolCall, PlannerTurn
from policy_api.tools.models import ToolAuditEvent, ToolConfirmation, ToolInvocation
from policy_api.slot_extraction.fingerprint import SlotExtractionFingerprinter
from policy_api.slot_extraction.repository import SlotExtractionOperationRepository
from policy_api.slot_extraction.schemas import SlotCandidate, SlotExtractionEnvelope
from policy_api.slot_extraction.service import SlotExtractionService
from policy_api.workbench.audit import SecurityAuditEvent
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityResolver,
    OrganizationUnit,
    ScopeKind,
)
from policy_api.workbench.events import ProductEvent, ProductEventEmitter
from tests.integration.test_procurement_submission_transactions import (
    FailingEventEmitter,
    ProcurementFixture,
    aggregate_counts,
    procurement_fixture,
)


def request_input(*, title: str = "研发采购") -> ProcurementRequestInput:
    return ProcurementRequestInput.model_validate(
        {
            "title": title,
            "purpose": "研发环境升级",
            "needed_by_date": "2030-02-01",
            "currency": "CNY",
            "items": [
                {
                    "category_code": "it_equipment",
                    "item_name": "开发工作站",
                    "specification": "64 GB",
                    "quantity": "2.00",
                    "unit": "台",
                    "estimated_unit_price": "12000.00",
                },
                {
                    "category_code": "software_service",
                    "item_name": "开发许可",
                    "specification": None,
                    "quantity": "3",
                    "unit": "套",
                    "estimated_unit_price": "500.00",
                },
            ],
        },
        context={"today": datetime(2026, 8, 24, tzinfo=timezone.utc).date()},
    )


def normalized(value: ProcurementRequestInput) -> dict[str, object]:
    return value.model_dump(mode="json")


class RecordingService:
    def __init__(self, target) -> None:
        self.target = target
        self.submit_calls: list[dict[str, object]] = []

    def submit_procurement_request(self, db: Session, **kwargs):
        self.submit_calls.append(dict(kwargs))
        return self.target.submit_procurement_request(db, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self.target, name)


class FailAtLifecycleEmitter(ProductEventEmitter):
    def __init__(self, fail_at: int) -> None:
        self._fail_at = fail_at
        self._calls = 0

    def append(self, db: Session, event_input):
        self._calls += 1
        if self._calls == self._fail_at:
            raise RuntimeError(f"lifecycle_event_failure_{self._fail_at}")
        return super().append(db, event_input)


def runtime(
    fixture: ProcurementFixture,
    *,
    service=None,
    observability: ProcurementObservability | None = None,
) -> ProcurementRuntime:
    return ProcurementRuntime(
        service=service or fixture.service(),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        observability=observability,
    )


class StaticSlotClient:
    model = "test-procurement-slot-extractor"

    def __init__(self, envelope: SlotExtractionEnvelope) -> None:
        self._envelope = envelope
        self.calls = 0

    def extract(self, *, current_user_turn_text, slot_schema):  # type: ignore[no-untyped-def]
        del current_user_turn_text, slot_schema
        self.calls += 1
        return self._envelope


def slot_service(envelope: SlotExtractionEnvelope) -> SlotExtractionService:
    return SlotExtractionService(
        client=StaticSlotClient(envelope),
        repository=SlotExtractionOperationRepository(),
        fingerprinter=SlotExtractionFingerprinter(
            b"test-procurement-slot-secret" * 2
        ),
        model_timeout_seconds=30,
    )


def pending_confirmation(
    db: Session,
    fixture: ProcurementFixture,
    value: ProcurementRequestInput,
    *,
    expires_at: datetime | None = None,
) -> ToolConfirmation:
    invocation = ToolInvocation(
        conversation_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        actor_user_id=fixture.applicant_id,
        provider_call_id=f"procurement-{uuid.uuid4()}",
        tool_name="procurement.submit_request",
        provider_tool_name="procurement_submit_request",
        risk_level="write",
        status=ToolInvocationStatus.PROPOSED,
        arguments_hash=canonical_arguments_hash(normalized(value)),
    )
    db.add(invocation)
    db.flush()
    confirmation = create_tool_confirmation(
        db,
        invocation=invocation,
        owner_user_id=fixture.applicant_id,
        normalized_arguments=normalized(value),
        preview={"type": "procurement_request", "title": value.title},
        expires_at=expires_at
        or datetime.now(timezone.utc) + timedelta(minutes=10),
        audit_summary=AuditSummary.for_execution(
            tool_name=invocation.tool_name,
            risk_level="write",
            outcome="confirmation_pending",
        ),
    )
    db.commit()
    return confirmation


def pending_write_confirmation(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    tool_name: str,
    arguments: dict[str, object],
) -> ToolConfirmation:
    invocation = ToolInvocation(
        conversation_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        actor_user_id=actor_user_id,
        provider_call_id=f"{tool_name}-{uuid.uuid4()}",
        tool_name=tool_name,
        provider_tool_name=tool_name.replace(".", "_"),
        risk_level="write",
        status=ToolInvocationStatus.PROPOSED,
        arguments_hash=canonical_arguments_hash(arguments),
    )
    db.add(invocation)
    db.flush()
    confirmation = create_tool_confirmation(
        db,
        invocation=invocation,
        owner_user_id=actor_user_id,
        normalized_arguments=arguments,
        preview={"type": "procurement_request"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        audit_summary=AuditSummary.for_execution(
            tool_name=tool_name,
            risk_level="write",
            outcome="confirmation_pending",
        ),
    )
    db.commit()
    return confirmation


@pytest.fixture(autouse=True)
def cleanup_runtime_rows(procurement_fixture: ProcurementFixture):
    yield
    with procurement_fixture.sessions.begin() as db:
        invocation_ids = select(ToolInvocation.id).where(
            ToolInvocation.actor_user_id.in_(procurement_fixture.user_ids)
        )
        db.execute(delete(ToolAuditEvent).where(ToolAuditEvent.invocation_id.in_(invocation_ids)))
        db.execute(delete(ToolConfirmation).where(ToolConfirmation.owner_user_id.in_(procurement_fixture.user_ids)))
        db.execute(delete(ToolInvocation).where(ToolInvocation.actor_user_id.in_(procurement_fixture.user_ids)))
        db.execute(delete(AssistantTurn).where(AssistantTurn.owner_user_id == procurement_fixture.applicant_id))
        db.execute(delete(AssistantConversation).where(AssistantConversation.owner_user_id == procurement_fixture.applicant_id))


def test_approval_reader_pushes_capability_subtree_scope_into_sql_candidates(
    procurement_fixture: ProcurementFixture,
) -> None:
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    child = OrganizationUnit(
        id=uuid.uuid4(),
        code=f"{procurement_fixture.prefix}-CHILD",
        name="Authorized child",
        parent_id=procurement_fixture.organization_id,
        is_active=True,
    )
    other = OrganizationUnit(
        id=uuid.uuid4(),
        code=f"{procurement_fixture.prefix}-OTHER",
        name="Unauthorized unit",
        is_active=True,
    )

    def pending_capability_task(
        organization_unit_id: uuid.UUID,
    ) -> tuple[ApprovalInstance, ApprovalTask]:
        instance = ApprovalInstance(
            id=uuid.uuid4(),
            process_key=PROCUREMENT_REQUEST_V1.process_key,
            process_version=PROCUREMENT_REQUEST_V1.version,
            subject_type=SUBJECT_TYPE,
            applicant_user_id=procurement_fixture.applicant_id,
            organization_unit_id=organization_unit_id,
            status=ApprovalInstanceStatus.RUNNING,
            current_step_key="procurement_review",
            version=1,
            submitted_at=now,
        )
        task = ApprovalTask(
            id=uuid.uuid4(),
            instance_id=instance.id,
            sequence=2,
            step_key="procurement_review",
            step_label="采购专员复核",
            assignment_kind=AssignmentKind.CAPABILITY,
            assigned_user_id=None,
            required_capability=Capability.PROCUREMENT_FINAL_REVIEW.value,
            scope_organization_unit_id=organization_unit_id,
            status=ApprovalTaskStatus.PENDING,
            activated_at=now,
        )
        return instance, task

    authorized_instance, authorized_task = pending_capability_task(child.id)
    second_authorized_instance, second_authorized_task = (
        pending_capability_task(child.id)
    )
    unauthorized_instance, unauthorized_task = pending_capability_task(other.id)
    repository = ProcurementRepository()
    resolver = MagicMock(wraps=CapabilityResolver())
    access = ProcurementApprovalAccess(
        repository=repository,
        capability_resolver=resolver,
    )
    can_view = MagicMock(wraps=access.can_view)
    access.can_view = can_view  # type: ignore[method-assign]
    registry = MagicMock(spec=SubjectAdapterRegistry)
    registry.summary.return_value = SubjectSummary(
        request_number="PR-SCOPE-TEST",
        title="Scope test",
        total=Decimal("1.00"),
        status="pending_procurement",
    )
    reader = SqlAlchemyApprovalTaskReader(
        access=access,
        registry=registry,
    )
    final_review_grant = CapabilityGrant(
        id=uuid.uuid4(),
        user_id=procurement_fixture.manager_id,
        capability=Capability.PROCUREMENT_FINAL_REVIEW.value,
        scope_kind=ScopeKind.UNIT_SUBTREE.value,
        organization_unit_id=procurement_fixture.organization_id,
        is_active=True,
    )

    with procurement_fixture.sessions() as db:
        db.add_all(
            [
                child,
                other,
                final_review_grant,
            ]
        )
        db.flush()
        db.add_all(
            [
                authorized_instance,
                second_authorized_instance,
                unauthorized_instance,
            ]
        )
        db.flush()
        db.add_all(
            [
                authorized_task,
                second_authorized_task,
                unauthorized_task,
            ]
        )
        db.flush()
        candidates = tuple(
            reader.candidates(
                db,
                actor=procurement_fixture.actor(
                    db, procurement_fixture.manager_id
                ),
                status=ApprovalTaskStatus.PENDING.value,
                process_key=PROCUREMENT_REQUEST_V1.process_key,
                activated_from=None,
                activated_to=None,
            )
        )
        summaries = tuple(
            reader.summary(
                db,
                actor=procurement_fixture.actor(
                    db, procurement_fixture.manager_id
                ),
                candidate=candidate,
            )
            for candidate in candidates
        )
        mismatched_actor_summary = reader.summary(
            db,
            actor=procurement_fixture.actor(db),
            candidate=candidates[0],
        )
        final_review_grant.is_active = False
        db.flush()
        candidates_after_revocation = tuple(
            reader.candidates(
                db,
                actor=procurement_fixture.actor(
                    db, procurement_fixture.manager_id
                ),
                status=ApprovalTaskStatus.PENDING.value,
                process_key=PROCUREMENT_REQUEST_V1.process_key,
                activated_from=None,
                activated_to=None,
            )
        )
        db.rollback()

    assert {task.id for task, _instance, _decision in candidates} == {
        authorized_task.id,
        second_authorized_task.id,
    }
    assert all(summary is not None for summary in summaries)
    assert mismatched_actor_summary is None
    assert candidates_after_revocation == ()
    assert can_view.call_count == 3
    assert resolver.scope_for.call_count == 4


def test_manual_and_ai_confirmation_call_the_same_submission_service(
    procurement_fixture: ProcurementFixture,
) -> None:
    recorder = RecordingService(procurement_fixture.service())
    app_runtime = runtime(procurement_fixture, service=recorder)
    with procurement_fixture.sessions() as db:
        actor = procurement_fixture.actor(db)
        app_runtime.submit_request(
            db,
            actor=actor,
            client_operation_id=uuid.uuid4(),
            request_input=request_input(title="手工采购"),
            request_id="manual-trace",
        )
        confirmation = pending_confirmation(db, procurement_fixture, request_input(title="AI 采购"))
        app_runtime.confirm_submission(
            db,
            actor=actor,
            confirmation_id=confirmation.id,
            client_operation_id=uuid.uuid4(),
            request_id="ai-trace",
        )

    assert len(recorder.submit_calls) == 2
    assert recorder.submit_calls[0].get("commit", True) is True
    assert recorder.submit_calls[1]["commit"] is False
    assert [call["request_id"] for call in recorder.submit_calls] == ["manual-trace", "ai-trace"]


def test_ai_confirmation_has_one_outer_commit_and_all_resources_become_visible_together(
    procurement_fixture: ProcurementFixture,
) -> None:
    confirmation_id: uuid.UUID
    with procurement_fixture.sessions() as setup:
        confirmation_id = pending_confirmation(setup, procurement_fixture, request_input()).id

    observations: list[tuple[str, tuple[int, ...], ToolConfirmationStatus]] = []

    class ObservingSession(Session):
        commit_calls = 0

        def commit(self) -> None:
            self.commit_calls += 1
            with procurement_fixture.sessions() as outside:
                status = outside.get(ToolConfirmation, confirmation_id).status  # type: ignore[union-attr]
            observations.append(("before", aggregate_counts(procurement_fixture), status))
            super().commit()
            with procurement_fixture.sessions() as outside:
                status = outside.get(ToolConfirmation, confirmation_id).status  # type: ignore[union-attr]
            observations.append(("after", aggregate_counts(procurement_fixture), status))

    db = ObservingSession(
        bind=procurement_fixture.sessions.kw["bind"], expire_on_commit=False, autoflush=False
    )
    try:
        result = runtime(procurement_fixture).confirm_submission(
            db,
            actor=procurement_fixture.actor(db),
            confirmation_id=confirmation_id,
            client_operation_id=uuid.uuid4(),
            request_id="atomic-trace",
        )
        assert db.commit_calls == 1
        assert result["resource_id"] is not None
    finally:
        db.close()

    assert observations == [
        ("before", (0, 0, 0, 0, 0, 0, 0, 0), ToolConfirmationStatus.PENDING),
        ("after", (1, 1, 2, 1, 2, 0, 1, 1), ToolConfirmationStatus.CONSUMED),
    ]


def test_ai_command_failure_keeps_confirmation_pending_and_eight_business_resources_empty(
    procurement_fixture: ProcurementFixture,
) -> None:
    failing = procurement_fixture.service(product_event_emitter=FailingEventEmitter())
    with procurement_fixture.sessions() as db:
        confirmation = pending_confirmation(db, procurement_fixture, request_input())
        with pytest.raises(ToolError, match="tool_execution_failed"):
            runtime(procurement_fixture, service=failing).confirm_submission(
                db,
                actor=procurement_fixture.actor(db),
                confirmation_id=confirmation.id,
                client_operation_id=uuid.uuid4(),
                request_id="failure-trace",
            )
        db.refresh(confirmation)
        assert confirmation.status is ToolConfirmationStatus.PENDING
        assert confirmation.client_operation_id is None
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


def test_ai_final_approval_completed_event_failure_rolls_back_and_keeps_confirmation_pending(
    procurement_fixture: ProcurementFixture,
) -> None:
    repository = ProcurementRepository()
    access = ProcurementApprovalAccess(
        repository=repository,
        capability_resolver=CapabilityResolver(),
    )
    registry = SubjectAdapterRegistry([
        ProcurementSubjectAdapter(repository=repository),
    ])
    with procurement_fixture.sessions() as db:
        db.add(
            CapabilityGrant(
                id=uuid.uuid4(),
                user_id=procurement_fixture.manager_id,
                capability=Capability.PROCUREMENT_FINAL_REVIEW.value,
                scope_kind=ScopeKind.UNIT_SUBTREE.value,
                organization_unit_id=procurement_fixture.organization_id,
                is_active=True,
            )
        )
        db.commit()
        submitted = runtime(procurement_fixture).submit_request(
            db,
            actor=procurement_fixture.actor(db),
            client_operation_id=uuid.uuid4(),
            request_input=request_input(),
            request_id="ai-final-atomic-submit",
        )
        tasks = tuple(
            db.scalars(
                select(ApprovalTask)
                .join(
                    ProcurementRequest,
                    ProcurementRequest.approval_instance_id
                    == ApprovalTask.instance_id,
                )
                .where(ProcurementRequest.id == submitted["id"])
                .order_by(ApprovalTask.sequence)
            )
        )
        assert len(tasks) == 2
        ApprovalRuntime(registry=registry, access=access).approve_task(
            db,
            actor=procurement_fixture.actor(db, procurement_fixture.manager_id),
            task_id=tasks[0].id,
            client_operation_id=uuid.uuid4(),
            comment="department approved",
        )
        confirmation = pending_write_confirmation(
            db,
            actor_user_id=procurement_fixture.manager_id,
            tool_name="approval.approve_task",
            arguments={"task_id": str(tasks[1].id), "comment": "final approved"},
        )
        instance_id = tasks[1].instance_id
        confirmation_id = confirmation.id
        before = (
            db.get(ApprovalInstance, instance_id).status,  # type: ignore[union-attr]
            tuple(task.status for task in tasks),
            db.scalar(select(func.count()).select_from(ApprovalDecision)),
            db.scalar(select(func.count()).select_from(SecurityAuditEvent)),
            db.scalar(select(func.count()).select_from(ProductEvent)),
        )

        observability = ProcurementObservability(
            repository,
            FailAtLifecycleEmitter(2),
        )
        core = ApprovalRuntime(
            registry=registry,
            access=access,
            transition_observer=observability.stage_transition,
        )
        approval_runtime = ApprovalApiRuntime(
            core,
            reader=SqlAlchemyApprovalTaskReader(
                access=access,
                registry=registry,
            ),
        )
        app_runtime = ProcurementRuntime(
            service=procurement_fixture.service(observability=observability),
            capability_resolver=CapabilityResolver(),
            request_reader=object(),  # type: ignore[arg-type]
            approval_runtime=approval_runtime,
            observability=observability,
        )

        with pytest.raises(ToolError, match="tool_execution_failed"):
            app_runtime.confirm_submission(
                db,
                actor=procurement_fixture.actor(db, procurement_fixture.manager_id),
                confirmation_id=confirmation.id,
                client_operation_id=uuid.uuid4(),
                request_id="ai-final-completed-event-failure",
            )

    with procurement_fixture.sessions() as verification:
        stored_confirmation = verification.get(ToolConfirmation, confirmation_id)
        instance = verification.get(ApprovalInstance, instance_id)
        stored_tasks = tuple(
            verification.scalars(
                select(ApprovalTask)
                .where(ApprovalTask.instance_id == instance_id)
                .order_by(ApprovalTask.sequence)
            )
        )
        after = (
            instance.status,  # type: ignore[union-attr]
            tuple(task.status for task in stored_tasks),
            verification.scalar(select(func.count()).select_from(ApprovalDecision)),
            verification.scalar(select(func.count()).select_from(SecurityAuditEvent)),
            verification.scalar(select(func.count()).select_from(ProductEvent)),
        )
        assert stored_confirmation is not None
        assert stored_confirmation.status is ToolConfirmationStatus.PENDING
        assert stored_confirmation.client_operation_id is None
        assert after == before


def test_ai_confirmation_exact_replay_and_cross_confirmation_payload_conflict(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    observability = ProcurementObservability(ProcurementRepository())
    recorder = RecordingService(
        procurement_fixture.service(observability=observability)
    )
    app_runtime = runtime(
        procurement_fixture,
        service=recorder,
        observability=observability,
    )
    with procurement_fixture.sessions() as db:
        actor = procurement_fixture.actor(db)
        first_confirmation = pending_confirmation(db, procurement_fixture, request_input())
        first_confirmation_id = first_confirmation.id
        first_invocation_id = first_confirmation.invocation_id
        first = app_runtime.confirm_submission(
            db, actor=actor, confirmation_id=first_confirmation.id,
            client_operation_id=operation_id, request_id="first-trace",
        )
        replay = app_runtime.confirm_submission(
            db, actor=actor, confirmation_id=first_confirmation.id,
            client_operation_id=operation_id, request_id="replay-trace",
        )
        other = pending_confirmation(db, procurement_fixture, request_input(title="不同 payload"))
        other_confirmation_id = other.id
        with pytest.raises(ToolError, match="operation_id_conflict"):
            app_runtime.confirm_submission(
                db, actor=actor, confirmation_id=other.id,
                client_operation_id=operation_id, request_id="conflict-trace",
            )

    assert first["resource_id"] == replay["resource_id"]
    assert replay["replayed"] is True
    assert len(recorder.submit_calls) == 1
    assert aggregate_counts(procurement_fixture) == (1, 1, 2, 1, 2, 0, 1, 1)
    with procurement_fixture.sessions() as verification:
        assert (
            verification.scalar(
                select(func.count())
                .select_from(SecurityAuditEvent)
                .where(
                    SecurityAuditEvent.actor_user_id
                    == procurement_fixture.applicant_id,
                    SecurityAuditEvent.event_name
                    == "procurement_confirmation_confirmed",
                )
            )
            == 1
        )
        confirmed_audit = verification.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.actor_user_id
                == procurement_fixture.applicant_id,
                SecurityAuditEvent.event_name
                == "procurement_confirmation_confirmed",
            )
        )
        assert confirmed_audit is not None
        assert confirmed_audit.request_id == "first-trace"
        confirmed_event = verification.scalar(
            select(ProductEvent).where(
                ProductEvent.actor_user_id == procurement_fixture.applicant_id,
                ProductEvent.event_name == "confirmation_confirmed",
            )
        )
        assert confirmed_event is not None
        assert confirmed_event.request_id == "first-trace"
        replay_audit = verification.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.actor_user_id
                == procurement_fixture.applicant_id,
                SecurityAuditEvent.event_name
                == "procurement_operation_replayed",
            )
        )
        assert replay_audit is not None
        assert replay_audit.request_id == "replay-trace"
        assert replay_audit.outcome == "replayed"
        assert (
            verification.scalar(
                select(func.count())
                .select_from(ProductEvent)
                .where(
                    ProductEvent.actor_user_id == procurement_fixture.applicant_id,
                    ProductEvent.event_name == "confirmation_confirmed",
                )
            )
            == 1
        )
        assert (
            verification.scalar(
                select(func.count())
                .select_from(ToolAuditEvent)
                .where(
                    ToolAuditEvent.invocation_id == first_invocation_id,
                    ToolAuditEvent.event_kind == "execution_failed",
                )
            )
            == 0
        )
        stored = verification.get(ToolConfirmation, first_confirmation_id)
        assert stored is not None
        assert stored.status is ToolConfirmationStatus.CONSUMED
        other_stored = verification.get(ToolConfirmation, other_confirmation_id)
        assert other_stored is not None
        assert other_stored.status is ToolConfirmationStatus.PENDING
        assert other_stored.client_operation_id is None
        assert (
            verification.scalar(
                select(func.count())
                .select_from(SecurityAuditEvent)
                .where(
                    SecurityAuditEvent.actor_user_id
                    == procurement_fixture.applicant_id,
                    SecurityAuditEvent.event_name
                    == "procurement_operation_conflict",
                )
            )
            == 1
        )
        assert (
            verification.scalar(
                select(func.count())
                .select_from(ProductEvent)
                .where(
                    ProductEvent.actor_user_id == procurement_fixture.applicant_id,
                    ProductEvent.event_name == "procurement_flow_error",
                )
            )
            == 1
        )


def test_procurement_conversations_are_owner_scoped_and_turns_are_idempotent(
    procurement_fixture: ProcurementFixture,
) -> None:
    app_runtime = runtime(procurement_fixture)
    with procurement_fixture.sessions() as db:
        conversation = app_runtime.create_conversation(
            db, procurement_fixture.applicant_id, " 采购计划 "
        )
        assert conversation["title"] == "采购计划"
        assert app_runtime.list_conversations(
            db, procurement_fixture.applicant_id
        ) == [conversation]
        client_turn_id = uuid.uuid4()
        first = app_runtime.run_turn(
            db,
            procurement_fixture.applicant_id,
            conversation["id"],  # type: ignore[arg-type]
            client_turn_id,
            "提交采购申请",
            request_id="turn-trace",
        )
        replay = app_runtime.run_turn(
            db,
            procurement_fixture.applicant_id,
            conversation["id"],  # type: ignore[arg-type]
            client_turn_id,
            "提交采购申请",
            request_id="turn-replay",
        )
        assert first["replayed"] is False
        assert replay["replayed"] is True
        assert first["id"] == replay["id"]
        with pytest.raises(ToolError, match="client_turn_id_conflict"):
            app_runtime.run_turn(
                db,
                procurement_fixture.applicant_id,
                conversation["id"],  # type: ignore[arg-type]
                client_turn_id,
                "不同文本",
            )
        with pytest.raises(ToolError, match="procurement_conversation_not_found"):
            app_runtime.get_conversation(
                db, procurement_fixture.manager_id, conversation["id"]  # type: ignore[arg-type]
            )
        detail = app_runtime.get_conversation(
            db, procurement_fixture.applicant_id, conversation["id"]  # type: ignore[arg-type]
        )
        assert len(detail["turns"]) == 1  # type: ignore[arg-type]


def test_expired_confirm_and_cancel_are_committed_before_stable_error(
    procurement_fixture: ProcurementFixture,
) -> None:
    expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with procurement_fixture.sessions() as db:
        confirm_id = pending_confirmation(
            db, procurement_fixture, request_input(), expires_at=expired_at
        ).id
        cancel_id = pending_confirmation(
            db, procurement_fixture, request_input(), expires_at=expired_at
        ).id
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="confirmation_expired"):
            runtime(procurement_fixture).confirm_submission(
                db,
                actor=procurement_fixture.actor(db),
                confirmation_id=confirm_id,
                client_operation_id=uuid.uuid4(),
                request_id="expired-confirm",
            )
    with procurement_fixture.sessions() as verification:
        assert verification.get(ToolConfirmation, confirm_id).status is ToolConfirmationStatus.EXPIRED  # type: ignore[union-attr]
    with procurement_fixture.sessions() as db:
        with pytest.raises(ToolError, match="confirmation_expired"):
            runtime(procurement_fixture).cancel_confirmation(
                db,
                procurement_fixture.applicant_id,
                cancel_id,
                request_id="expired-cancel",
            )
    with procurement_fixture.sessions() as verification:
        assert verification.get(ToolConfirmation, cancel_id).status is ToolConfirmationStatus.EXPIRED  # type: ignore[union-attr]


@pytest.mark.parametrize("action", ["confirm", "cancel"])
@pytest.mark.parametrize("failure_layer", ["audit", "event"])
def test_expired_confirmation_evidence_failure_explicitly_rolls_back_everything(
    procurement_fixture: ProcurementFixture,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    failure_layer: str,
) -> None:
    expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with procurement_fixture.sessions() as setup:
        confirmation_id = pending_confirmation(
            setup,
            procurement_fixture,
            request_input(),
            expires_at=expired_at,
        ).id

    emitter = (
        FailAtLifecycleEmitter(1)
        if failure_layer == "event"
        else ProductEventEmitter()
    )
    observability = ProcurementObservability(ProcurementRepository(), emitter)
    if failure_layer == "audit":
        monkeypatch.setattr(
            "policy_api.procurement.observability.append_security_audit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("expired_audit_failure")
            ),
        )

    with procurement_fixture.sessions() as db:
        app_runtime = runtime(procurement_fixture, observability=observability)
        with pytest.raises(RuntimeError, match=f"expired_{failure_layer}_failure|lifecycle_event_failure_1"):
            if action == "confirm":
                app_runtime.confirm_submission(
                    db,
                    actor=procurement_fixture.actor(db),
                    confirmation_id=confirmation_id,
                    client_operation_id=uuid.uuid4(),
                    request_id=f"expired-{action}-{failure_layer}",
                )
            else:
                app_runtime.cancel_confirmation(
                    db,
                    procurement_fixture.applicant_id,
                    confirmation_id,
                    request_id=f"expired-{action}-{failure_layer}",
                )

        assert db.get(ToolConfirmation, confirmation_id).status is ToolConfirmationStatus.PENDING  # type: ignore[union-attr]
        assert db.scalar(
            select(func.count())
            .select_from(SecurityAuditEvent)
            .where(
                SecurityAuditEvent.actor_user_id
                == procurement_fixture.applicant_id,
                SecurityAuditEvent.event_name
                == "procurement_confirmation_expired",
            )
        ) == 0
        assert db.scalar(
            select(func.count())
            .select_from(ProductEvent)
            .where(
                ProductEvent.actor_user_id == procurement_fixture.applicant_id,
                ProductEvent.event_name == "confirmation_expired",
            )
        ) == 0

    with procurement_fixture.sessions() as verification:
        assert verification.get(ToolConfirmation, confirmation_id).status is ToolConfirmationStatus.PENDING  # type: ignore[union-attr]


def test_approval_failure_note_is_domain_neutral(
    procurement_fixture: ProcurementFixture,
) -> None:
    def failing_evidence(*_args, **_kwargs) -> None:
        raise RuntimeError("evidence_write_failed")

    core = ApprovalRuntime(
        registry=MagicMock(),
        access=MagicMock(),
        engine=MagicMock(),
        failure_observer=failing_evidence,
    )
    db = MagicMock(spec=Session)
    error = ToolError("approval_task_state_conflict")
    with procurement_fixture.sessions() as actor_db:
        actor = procurement_fixture.actor(actor_db)

    core._record_failure(
        db,
        error,
        actor,
        uuid.uuid4(),
        uuid.uuid4(),
        "server-attempt",
        "approval.approve",
    )

    assert error.__notes__ == [
        "approval/transition evidence failed: RuntimeError"
    ]
    db.rollback.assert_called_once_with()


def test_unknown_ai_failure_records_redacted_compensation_after_business_rollback(
    procurement_fixture: ProcurementFixture,
) -> None:
    failing = procurement_fixture.service(product_event_emitter=FailingEventEmitter())
    with procurement_fixture.sessions() as db:
        confirmation = pending_confirmation(db, procurement_fixture, request_input())
        confirmation_id = confirmation.id
        invocation_id = confirmation.invocation_id
        with pytest.raises(ToolError, match="tool_execution_failed"):
            runtime(procurement_fixture, service=failing).confirm_submission(
                db,
                actor=procurement_fixture.actor(db),
                confirmation_id=confirmation_id,
                client_operation_id=uuid.uuid4(),
                request_id="compensation-trace",
            )
    with procurement_fixture.sessions() as verification:
        confirmation = verification.get(ToolConfirmation, confirmation_id)
        invocation = verification.get(ToolInvocation, invocation_id)
        failures = tuple(
            verification.scalars(
                select(ToolAuditEvent).where(
                    ToolAuditEvent.invocation_id == invocation_id,
                    ToolAuditEvent.event_kind == "execution_failed",
                )
            )
        )
        assert confirmation is not None and confirmation.status is ToolConfirmationStatus.PENDING
        assert invocation is not None and invocation.status is ToolInvocationStatus.FAILED
        assert len(failures) == 1
        serialized = repr(failures[0].summary)
        for secret in (
            "研发采购", "研发环境升级", "开发工作站", "64 GB",
            "product event failed",
        ):
            assert secret not in serialized
    assert aggregate_counts(procurement_fixture) == (0, 0, 0, 0, 0, 0, 0, 0)


def test_concurrent_turn_claim_replays_same_canonical_text_and_conflicts_other_payload(
    procurement_fixture: ProcurementFixture,
) -> None:
    app_runtime = runtime(procurement_fixture)
    with procurement_fixture.sessions() as db:
        first_conversation = app_runtime.create_conversation(
            db, procurement_fixture.applicant_id, "first"
        )["id"]
        second_conversation = app_runtime.create_conversation(
            db, procurement_fixture.applicant_id, "second"
        )["id"]

    def race(
        conversation_ids: tuple[uuid.UUID, uuid.UUID], texts: tuple[str, str]
    ) -> list[tuple[str, object]]:
        turn_id = uuid.uuid4()
        barrier = Barrier(2)

        def run(index: int) -> tuple[str, object]:
            barrier.wait(timeout=10)
            with procurement_fixture.sessions() as db:
                try:
                    value = runtime(procurement_fixture).run_turn(
                        db,
                        procurement_fixture.applicant_id,
                        conversation_ids[index],
                        turn_id,
                        texts[index],
                    )
                    return "ok", value
                except ToolError as exc:
                    return "error", exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            return [future.result(timeout=20) for future in (
                executor.submit(run, 0), executor.submit(run, 1)
            )]

    same = race(
        (first_conversation, first_conversation),  # type: ignore[arg-type]
        ("  同一文本  ", "同一文本"),
    )
    assert [kind for kind, _ in same] == ["ok", "ok"]
    same_values = [value for _, value in same]
    assert {value["id"] for value in same_values}.__len__() == 1  # type: ignore[index]
    assert sorted(value["replayed"] for value in same_values) == [False, True]  # type: ignore[index]

    different = race(
        (first_conversation, second_conversation),  # type: ignore[arg-type]
        ("payload-a", "payload-b"),
    )
    assert sum(kind == "ok" for kind, _ in different) == 1
    assert ("error", "client_turn_id_conflict") in different


def test_concurrent_cross_confirmation_operation_has_one_winner_and_stable_conflict(
    procurement_fixture: ProcurementFixture,
) -> None:
    operation_id = uuid.uuid4()
    with procurement_fixture.sessions() as setup:
        confirmation_ids = (
            pending_confirmation(setup, procurement_fixture, request_input()).id,
            pending_confirmation(setup, procurement_fixture, request_input()).id,
        )
    barrier = Barrier(2)

    def run(index: int) -> tuple[str, object]:
        barrier.wait(timeout=10)
        with procurement_fixture.sessions() as db:
            try:
                value = runtime(procurement_fixture).confirm_submission(
                    db,
                    actor=procurement_fixture.actor(db),
                    confirmation_id=confirmation_ids[index],
                    client_operation_id=operation_id,
                    request_id=f"cross-{index}",
                )
                return "ok", value
            except ToolError as exc:
                return "error", exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=30) for future in (
            executor.submit(run, 0), executor.submit(run, 1)
        )]
    assert sum(kind == "ok" for kind, _ in results) == 1
    assert ("error", "operation_id_conflict") in results
    assert aggregate_counts(procurement_fixture) == (1, 1, 2, 1, 2, 0, 1, 1)
    with procurement_fixture.sessions() as verification:
        confirmations = tuple(
            verification.scalars(
                select(ToolConfirmation).where(ToolConfirmation.id.in_(confirmation_ids))
            )
        )
        assert sorted(item.status.value for item in confirmations) == ["consumed", "pending"]
        consumed = next(item for item in confirmations if item.status is ToolConfirmationStatus.CONSUMED)
        assert consumed.client_operation_id == operation_id


def test_runtime_builds_procurement_flow_orchestrator_without_executing_hidden_write(
    procurement_fixture: ProcurementFixture,
) -> None:
    class Planner:
        model = "test-procurement-planner"

        def __init__(self) -> None:
            self.tools_seen: list[list[dict[str, object]]] = []

        def complete(  # type: ignore[no-untyped-def]
            self, _messages, *, tools, required_tool_name=None
        ):
            del required_tool_name
            self.tools_seen.append(list(tools))
            return PlannerTurn(text="需要补充用途和明细", tool_calls=())

    planner = Planner()
    app_runtime = ProcurementRuntime(
        service=procurement_fixture.service(),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=planner,  # type: ignore[arg-type]
        approval_runtime=object(),  # type: ignore[arg-type]
    )
    with procurement_fixture.sessions() as db:
        orchestrator = app_runtime._orchestrator(db, None)  # type: ignore[arg-type]
        response = orchestrator.run(
            "提交采购申请，标题是研发采购",
            ToolContext(
                actor_user_id=procurement_fixture.applicant_id,
                role=procurement_fixture.actor(db).role,
            ),
        )

    assert response.blocks[-1].text == "需要补充用途和明细"
    assert planner.tools_seen == [[]]
    assert response.write_proposals == 0


def test_runtime_submit_proposal_persists_confirmation_with_request_trace(
    procurement_fixture: ProcurementFixture,
) -> None:
    class Planner:
        model = "test-procurement-planner"

        def __init__(self) -> None:
            self.calls = 0

        def complete(  # type: ignore[no-untyped-def]
            self, _messages, *, tools, required_tool_name=None
        ):
            del tools, required_tool_name
            self.calls += 1
            if self.calls == 1:
                return PlannerTurn(None, (
                    PlannedToolCall(
                        call_id="calculate",
                        name="procurement_calculate_request_total",
                        arguments={
                            "items": [{
                                "quantity": 2,
                                "estimated_unit_price": 399.5,
                            }],
                        },
                    ),
                ))
            return PlannerTurn(None, (
                PlannedToolCall(
                    call_id="submit",
                    name="procurement_submit_request",
                    arguments={
                        "title": "人体工学键盘采购",
                        "purpose": "研发环境升级",
                        "needed_by_date": "2030-02-01",
                        "currency": "CNY",
                        "items": [{
                            "category_code": "it_equipment",
                            "item_name": "人体工学键盘",
                            "specification": "虚构演示规格",
                            "quantity": 2,
                            "unit": "套",
                            "estimated_unit_price": 399.5,
                        }],
                    },
                ),
            ))

    observability = ProcurementObservability(ProcurementRepository())
    app_runtime = ProcurementRuntime(
        service=procurement_fixture.service(observability=observability),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=Planner(),  # type: ignore[arg-type]
        slot_extraction=slot_service(SlotExtractionEnvelope(
            schema_version=PROCUREMENT_SLOT_SCHEMA.version,
            candidates=[
                SlotCandidate(
                    slot_name="title",
                    raw_value="人体工学键盘采购",
                    source_quote="标题：人体工学键盘采购",
                ),
                SlotCandidate(
                    slot_name="purpose",
                    raw_value="研发环境升级",
                    source_quote="用途：研发环境升级",
                ),
                SlotCandidate(
                    slot_name="needed_by_date",
                    raw_value="2030-02-01",
                    source_quote="需要日期 2030-02-01",
                ),
                SlotCandidate(
                    slot_name="currency",
                    raw_value="CNY",
                    source_quote="币种 CNY",
                ),
                SlotCandidate(
                    slot_name="items",
                    raw_value={
                        "item_name": "人体工学键盘",
                        "specification": "虚构演示规格",
                        "quantity": "2",
                        "unit": "套",
                        "estimated_unit_price": "399.50",
                        "category_hint": "it_equipment",
                    },
                    source_quote=(
                        "人体工学键盘，分类：it_equipment，规格：虚构演示规格，"
                        "2套，单价399.50元"
                    ),
                ),
            ],
        )),
        approval_runtime=object(),  # type: ignore[arg-type]
        observability=observability,
    )
    trace_id = f"task17-ai-proposal-{uuid.uuid4()}"

    with procurement_fixture.sessions() as db:
        before_confirmation_ids = set(db.scalars(select(ToolConfirmation.id)))
        conversation_id = app_runtime.create_conversation(
            db, procurement_fixture.applicant_id, "proposal trace"
        )["id"]
        result = app_runtime.run_turn(
            db,
            procurement_fixture.applicant_id,
            conversation_id,  # type: ignore[arg-type]
            uuid.uuid4(),
            "请提交采购申请，标题：人体工学键盘采购；用途：研发环境升级；"
            "需要日期 2030-02-01；币种 CNY；明细：人体工学键盘，"
            "分类：it_equipment，规格：虚构演示规格，2套，单价399.50元。",
            request_id=trace_id,
        )

        confirmations = tuple(db.scalars(
            select(ToolConfirmation).where(
                ToolConfirmation.id.not_in(before_confirmation_ids)
            )
        ))
        shown = tuple(db.scalars(select(ProductEvent).where(
            ProductEvent.event_name == "confirmation_shown",
            ProductEvent.request_id == trace_id,
        )))

    assert any(block["type"] == "confirmation" for block in result["blocks"])
    assert len(confirmations) == 1
    assert len(shown) == 1
    assert shown[0].request_id == trace_id


def test_procurement_runtime_guards_model_approval_advice_but_preserves_facts(
    procurement_fixture: ProcurementFixture,
) -> None:
    class Planner:
        model = "test-procurement-planner"

        def __init__(self, text: str) -> None:
            self.text = text
            self.messages_seen: list[list[dict[str, object]]] = []

        def complete(  # type: ignore[no-untyped-def]
            self, messages, *, tools, required_tool_name=None
        ):
            del tools, required_tool_name
            self.messages_seen.append([dict(item) for item in messages])
            return PlannerTurn(text=self.text, tool_calls=())

    with procurement_fixture.sessions() as db:
        advice = Planner("系统建议批准这条申请。")
        guarded_runtime = ProcurementRuntime(
            service=procurement_fixture.service(),
            capability_resolver=CapabilityResolver(),
            request_reader=object(),  # type: ignore[arg-type]
            planner=advice,  # type: ignore[arg-type]
            approval_runtime=object(),  # type: ignore[arg-type]
        )
        guarded = guarded_runtime._orchestrator(db, None).run(
            "这条申请应该通过吗？",
            ToolContext(
                actor_user_id=procurement_fixture.applicant_id,
                role=procurement_fixture.actor(db).role,
            ),
        )
        facts = Planner("该采购申请当前总额为 24000 元。")
        facts_runtime = ProcurementRuntime(
            service=procurement_fixture.service(),
            capability_resolver=CapabilityResolver(),
            request_reader=object(),  # type: ignore[arg-type]
            planner=facts,  # type: ignore[arg-type]
            approval_runtime=object(),  # type: ignore[arg-type]
        )
        ordinary = facts_runtime._orchestrator(db, None).run(
            "说明采购事实",
            ToolContext(
                actor_user_id=procurement_fixture.applicant_id,
                role=procurement_fixture.actor(db).role,
            ),
        )

    assert guarded.blocks[-1].text == "我不能替审批人作出批准或拒绝决定；请您依据申请事实和制度自行判断。"
    assert ordinary.blocks[-1].text == "该采购申请当前总额为 24000 元。"
    assert "不得建议批准或拒绝" in advice.messages_seen[0][0]["content"]


@pytest.mark.parametrize(
    ("text", "provider_tool"),
    (
        ("不要提交采购申请", "procurement_submit_request"),
        (
            "别撤回采购申请 11111111-1111-4111-8111-111111111111",
            "procurement_withdraw_request",
        ),
        ("无需批准我的待审批任务", "approval_approve_task"),
        (
            "不必拒绝任务 11111111-1111-4111-8111-111111111111",
            "approval_reject_task",
        ),
        ("不提交采购申请", "procurement_submit_request"),
        (
            "暂不撤回采购申请 11111111-1111-4111-8111-111111111111",
            "procurement_withdraw_request",
        ),
        ("不能批准最新待审批任务", "approval_approve_task"),
        (
            "不可拒绝任务 11111111-1111-4111-8111-111111111111",
            "approval_reject_task",
        ),
        ("请勿提交采购申请", "procurement_submit_request"),
        (
            "并非要撤回采购申请 11111111-1111-4111-8111-111111111111",
            "procurement_withdraw_request",
        ),
        ("不是要批准最新待审批任务", "approval_approve_task"),
        (
            "不想拒绝任务 11111111-1111-4111-8111-111111111111",
            "approval_reject_task",
        ),
        ("不打算提交采购申请", "procurement_submit_request"),
        ("没打算提交采购申请", "procurement_submit_request"),
        (
            "没有打算撤回采购申请 11111111-1111-4111-8111-111111111111",
            "procurement_withdraw_request",
        ),
        ("无意批准最新待审批任务", "approval_approve_task"),
        (
            "尚未决定拒绝任务 11111111-1111-4111-8111-111111111111",
            "approval_reject_task",
        ),
        ("还没决定提交采购申请", "procurement_submit_request"),
        (
            "尚未想好是否撤回采购申请 11111111-1111-4111-8111-111111111111",
            "procurement_withdraw_request",
        ),
        ("还没想好批准最新待审批任务", "approval_approve_task"),
        ("还没有决定提交采购申请", "procurement_submit_request"),
        (
            "暂未决定撤回采购申请 11111111-1111-4111-8111-111111111111",
            "procurement_withdraw_request",
        ),
        ("还没有想好批准最新待审批任务", "approval_approve_task"),
        (
            "暂未想好拒绝任务 11111111-1111-4111-8111-111111111111",
            "approval_reject_task",
        ),
        ("我还在犹豫是否批准最新采购申请", "approval_approve_task"),
        (
            "我没有确定要不要批准任务 11111111-1111-4111-8111-111111111111",
            "approval_approve_task",
        ),
        (
            "尚未确认是否拒绝任务 11111111-1111-4111-8111-111111111111",
            "approval_reject_task",
        ),
        (
            "我还没做最终决定，要批准任务 11111111-1111-4111-8111-111111111111",
            "approval_approve_task",
        ),
        (
            "我正在考虑撤回采购申请 11111111-1111-4111-8111-111111111111",
            "procurement_withdraw_request",
        ),
        (
            "我反对提交采购申请：标题：电脑；用途：研发；需要日期：2030-01-01；币种：人民币；明细：电脑 1 台，单价 1 元。",
            "procurement_submit_request",
        ),
        (
            "制度禁止批准任务 11111111-1111-4111-8111-111111111111",
            "approval_approve_task",
        ),
    ),
)
def test_negated_write_intent_is_fail_closed_end_to_end_with_zero_persist(
    procurement_fixture: ProcurementFixture,
    text: str,
    provider_tool: str,
) -> None:
    class Planner:
        model = "malicious-test-planner"

        def __init__(self) -> None:
            self.tools_seen: list[list[dict[str, object]]] = []

        def complete(  # type: ignore[no-untyped-def]
            self, _messages, *, tools, required_tool_name=None
        ):
            del required_tool_name
            self.tools_seen.append(list(tools))
            return PlannerTurn(
                None,
                (
                    PlannedToolCall(
                        call_id="negated-write",
                        name=provider_tool,
                        arguments={},
                    ),
                ),
            )

    planner = Planner()
    app_runtime = ProcurementRuntime(
        service=procurement_fixture.service(),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=planner,  # type: ignore[arg-type]
        approval_runtime=object(),  # type: ignore[arg-type]
    )
    with procurement_fixture.sessions() as db:
        before = db.scalar(
            select(func.count())
            .select_from(ToolConfirmation)
            .where(ToolConfirmation.owner_user_id == procurement_fixture.applicant_id)
        )
        result = app_runtime._orchestrator(db, None).run(
            text,
            ToolContext(
                actor_user_id=procurement_fixture.applicant_id,
                role=procurement_fixture.actor(db).role,
            ),
        )
        after = db.scalar(
            select(func.count())
            .select_from(ToolConfirmation)
            .where(ToolConfirmation.owner_user_id == procurement_fixture.applicant_id)
        )

    assert planner.tools_seen == [[]]
    assert result.blocks[-1].code == "tool_not_available_in_flow"
    assert result.write_proposals == 0
    assert before == after == 0


def test_policy_permission_query_searches_then_rejects_hidden_approval_with_zero_persist(
    procurement_fixture: ProcurementFixture,
) -> None:
    class Planner:
        model = "malicious-policy-test-planner"

        def __init__(self) -> None:
            self.tools_seen: list[list[dict[str, object]]] = []

        def complete(  # type: ignore[no-untyped-def]
            self, _messages, *, tools, required_tool_name=None
        ):
            del required_tool_name
            self.tools_seen.append(list(tools))
            if len(self.tools_seen) == 1:
                return PlannerTurn(
                    None,
                    (
                        PlannedToolCall(
                            call_id="policy-read",
                            name="knowledge_search_policy",
                            arguments={"query": "采购制度允许我批准这个任务吗？"},
                        ),
                    ),
                )
            return PlannerTurn(
                None,
                (
                    PlannedToolCall(
                        call_id="hidden-approve",
                        name="approval_approve_task",
                        arguments={},
                    ),
                ),
            )

    planner = Planner()
    app_runtime = ProcurementRuntime(
        service=procurement_fixture.service(),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        planner=planner,  # type: ignore[arg-type]
        approval_runtime=object(),  # type: ignore[arg-type]
        search_policy=lambda _db, _query: PolicySearchOutcome(
            status="answered",
            text="审批权限以采购制度和任务授权为准。",
            refusal_reason=None,
            citations=(),
        ),
    )
    with procurement_fixture.sessions() as db:
        before = db.scalar(
            select(func.count())
            .select_from(ToolConfirmation)
            .where(ToolConfirmation.owner_user_id == procurement_fixture.applicant_id)
        )
        result = app_runtime._orchestrator(db, None).run(
            "采购制度允许我批准这个任务吗？",
            ToolContext(
                actor_user_id=procurement_fixture.applicant_id,
                role=procurement_fixture.actor(db).role,
            ),
        )
        after = db.scalar(
            select(func.count())
            .select_from(ToolConfirmation)
            .where(ToolConfirmation.owner_user_id == procurement_fixture.applicant_id)
        )

    assert [tool["function"]["name"] for tool in planner.tools_seen[0]] == [
        "knowledge_search_policy"
    ]
    assert planner.tools_seen[1] == []
    assert result.blocks[-1].code == "tool_not_available_in_flow"
    assert result.write_proposals == 0
    assert before == after == 0


@pytest.mark.parametrize(
    "dangerous",
    (
        "建议批准该申请。",
        "该申请可以批准。",
        "该申请可予批准。",
        "应予通过。",
        "该申请具备通过条件。",
        "该申请符合拒绝条件。",
        "I recommend approving this request.",
        "This request should be rejected.",
        "This request can be approved.",
        "我倾向于批准该申请。",
        "该申请适合批准。",
        "批准该申请是合理的。",
        "该申请可通过。",
        "该申请值得批准。",
        "该申请不宜批准。",
        "I advise approving this request.",
        "I advise rejection of this request.",
        "This request is fit for approval.",
        "Approval would be appropriate.",
        "Approval is warranted.",
        "It is appropriate to approve this request.",
        "The evidence warrants approval.",
        "I would approve this request.",
        "I would reject this request.",
        "经理建议批准该申请。",
        "The manager recommends approving this request.",
        "经理此前建议批准，但我倾向于批准该申请。",
        "The manager recommended approval yesterday, but I would approve it.",
        "经理此前建议批准并且我现在倾向于批准该申请。",
        "经理此前建议批准以及我现在认为应该批准该申请。",
        "经理此前建议批准同时我现在认为该申请适合批准。",
        "The reviewer previously advised rejection and I would reject this request.",
    ),
)
def test_procurement_text_guard_replaces_decision_advice_sentences(dangerous: str) -> None:
    original = f"申请总额为 24000 元。{dangerous}当前状态为待审批。"
    guarded = procurement_text_postcondition(original)
    assert guarded == (
        "申请总额为 24000 元。"
        "我不能替审批人作出批准或拒绝决定；请您依据申请事实和制度自行判断。"
        "当前状态为待审批。"
    )


@pytest.mark.parametrize(
    "facts",
    (
        "通过部门经理审批后进入采购复核。",
        "制度规定该申请应该通过部门经理审批后进入采购复核。",
        "该申请已经批准。",
        "当前状态为已拒绝。",
        "The request was approved yesterday.",
        "The current status is rejected.",
        "经理此前建议批准该申请。",
        "采购专员昨天认为该申请适合批准。",
        "系统记录显示该申请曾被建议拒绝。",
        "审批记录显示该申请随后已获批准。",
        "The manager recommended approving it yesterday.",
        "The reviewer previously advised rejecting it.",
        "The system record says approval was recommended previously.",
    ),
)
def test_procurement_text_guard_preserves_process_and_historical_facts(facts: str) -> None:
    assert procurement_text_postcondition(facts) == facts


def test_procurement_text_guard_preserves_line_breaks_and_leading_space() -> None:
    original = "申请总额为 24000 元。\n  建议批准该申请。\n当前状态为待审批。"
    assert procurement_text_postcondition(original) == (
        "申请总额为 24000 元。\n  "
        "我不能替审批人作出批准或拒绝决定；请您依据申请事实和制度自行判断。"
        "\n当前状态为待审批。"
    )


@pytest.mark.parametrize("decision", ("approve", "reject"))
def test_approval_proposal_executes_only_through_confirmation_dispatch(
    procurement_fixture: ProcurementFixture,
    decision: str,
) -> None:
    repository = ProcurementRepository()
    access = ProcurementApprovalAccess(
        repository=repository,
        capability_resolver=CapabilityResolver(),
    )
    subject_registry = SubjectAdapterRegistry([
        ProcurementSubjectAdapter(repository=repository),
    ])
    approval_runtime = ApprovalApiRuntime(
        ApprovalRuntime(registry=subject_registry, access=access),
        reader=SqlAlchemyApprovalTaskReader(
            access=access,
            registry=subject_registry,
        ),
    )
    app_runtime = ProcurementRuntime(
        service=procurement_fixture.service(),
        capability_resolver=CapabilityResolver(),
        request_reader=object(),  # type: ignore[arg-type]
        approval_runtime=approval_runtime,
    )
    with procurement_fixture.sessions() as db:
        submitted = app_runtime.submit_request(
            db,
            actor=procurement_fixture.actor(db),
            client_operation_id=uuid.uuid4(),
            request_input=request_input(),
            request_id=f"approval-confirm-{decision}",
        )
        task = db.scalar(
            select(ApprovalTask)
            .join(
                ProcurementRequest,
                ProcurementRequest.approval_instance_id == ApprovalTask.instance_id,
            )
            .where(
                ProcurementRequest.id == submitted["id"],
                ApprovalTask.status == "pending",
            )
        )
        assert task is not None
        tool_name = f"approval.{decision}_task"
        arguments: dict[str, object] = {"task_id": str(task.id)}
        if decision == "approve":
            arguments["comment"] = "预算合理"
        else:
            arguments["reason"] = "预算不合理"
        confirmation = pending_write_confirmation(
            db,
            actor_user_id=procurement_fixture.manager_id,
            tool_name=tool_name,
            arguments=arguments,
        )

        result = app_runtime.confirm_submission(
            db,
            actor=procurement_fixture.actor(db, procurement_fixture.manager_id),
            confirmation_id=confirmation.id,
            client_operation_id=uuid.uuid4(),
            request_id=f"approval-confirm-{decision}-execute",
        )
        db.refresh(task)

    assert result == {
        "type": "execution_result",
        "resource_type": "procurement_request",
        "resource_id": submitted["id"],
        "replayed": False,
    }
    assert task.status.value == ("approved" if decision == "approve" else "rejected")
