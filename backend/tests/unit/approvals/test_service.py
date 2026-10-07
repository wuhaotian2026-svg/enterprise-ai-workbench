from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from policy_api.approvals.definitions import (
    AssignmentKind,
    ProcessDefinition,
    StepAssignment,
    StepDefinition,
)
from policy_api.approvals.enums import (
    ApprovalCommandKind,
    ApprovalCommandStatus,
    ApprovalDecisionAction,
    ApprovalInstanceStatus,
    ApprovalTaskStatus,
)
from policy_api.approvals.models import (
    ApprovalCommandOperation,
    ApprovalDecision,
    ApprovalInstance,
    ApprovalTask,
)
from policy_api.models import User, UserRole
from policy_api.approvals.repository import ApprovalRepository
from policy_api.approvals.service import ApprovalEngine
from policy_api.tools.errors import ToolError

NOW = datetime(2026, 8, 23, 12, 30, tzinfo=timezone.utc)

PROCUREMENT_V1 = ProcessDefinition(
    process_key="procurement.request",
    version=1,
    steps=(
        StepDefinition(
            "department_manager_review",
            "部门负责人审批",
            1,
            AssignmentKind.USER,
            "procurement.department.review",
        ),
        StepDefinition(
            "procurement_review",
            "采购专员复核",
            2,
            AssignmentKind.CAPABILITY,
            "procurement.final.review",
        ),
    ),
)


def valid_assignments(
    manager_id: uuid.UUID, unit_id: uuid.UUID
) -> dict[str, StepAssignment]:
    return {
        "department_manager_review": StepAssignment.for_user(manager_id),
        "procurement_review": StepAssignment.for_capability(
            "procurement.final.review", unit_id
        ),
    }


def test_start_creates_one_running_instance_and_ordered_task_snapshots() -> None:
    db = MagicMock()
    manager_id = uuid.uuid4()
    applicant_id = uuid.uuid4()
    unit_id = uuid.uuid4()
    engine = ApprovalEngine(ApprovalRepository())

    instance, tasks = engine.start_instance(
        db,
        definition=PROCUREMENT_V1,
        subject_type="test_subject",
        applicant_user_id=applicant_id,
        organization_unit_id=unit_id,
        assignments=valid_assignments(manager_id, unit_id),
        now=NOW,
    )

    assert instance.status is ApprovalInstanceStatus.RUNNING
    assert instance.process_key == "procurement.request"
    assert instance.process_version == 1
    assert instance.subject_type == "test_subject"
    assert instance.applicant_user_id == applicant_id
    assert instance.organization_unit_id == unit_id
    assert instance.current_step_key == "department_manager_review"
    assert instance.submitted_at == NOW
    assert instance.completed_at is None

    assert [task.sequence for task in tasks] == [1, 2]
    assert [task.step_key for task in tasks] == [
        "department_manager_review",
        "procurement_review",
    ]
    assert [task.step_label for task in tasks] == ["部门负责人审批", "采购专员复核"]
    assert [task.status for task in tasks] == [
        ApprovalTaskStatus.PENDING,
        ApprovalTaskStatus.WAITING,
    ]
    assert [task.activated_at for task in tasks] == [NOW, None]
    assert tasks[0].assigned_user_id == manager_id
    assert tasks[0].required_capability is None
    assert tasks[0].scope_organization_unit_id is None
    assert tasks[1].assigned_user_id is None
    assert tasks[1].required_capability == "procurement.final.review"
    assert tasks[1].scope_organization_unit_id == unit_id
    assert all(task.instance_id == instance.id for task in tasks)

    assert db.add.call_count == 3
    db.flush.assert_called_once_with()
    db.commit.assert_not_called()


@pytest.mark.parametrize(
    "assignments",
    [
        {"department_manager_review": StepAssignment.for_user(uuid.uuid4())},
        {
            "department_manager_review": StepAssignment.for_user(uuid.uuid4()),
            "procurement_review": StepAssignment.for_capability(
                "procurement.final.review", uuid.uuid4()
            ),
            "unexpected": StepAssignment.for_user(uuid.uuid4()),
        },
    ],
)
def test_start_rejects_missing_or_extra_assignment_without_persistence(
    assignments: dict[str, StepAssignment],
) -> None:
    db = MagicMock()
    engine = ApprovalEngine(ApprovalRepository())

    with pytest.raises(ToolError) as error:
        engine.start_instance(
            db,
            definition=PROCUREMENT_V1,
            subject_type="test_subject",
            applicant_user_id=uuid.uuid4(),
            organization_unit_id=uuid.uuid4(),
            assignments=assignments,
            now=NOW,
        )

    assert error.value.code == "approval_assignment_mismatch"
    db.add.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.parametrize("mismatch", ["kind", "capability"])
def test_start_rejects_assignment_kind_or_capability_mismatch(mismatch: str) -> None:
    db = MagicMock()
    unit_id = uuid.uuid4()
    assignments = valid_assignments(uuid.uuid4(), unit_id)
    if mismatch == "kind":
        assignments["department_manager_review"] = StepAssignment.for_capability(
            "procurement.department.review", unit_id
        )
    else:
        assignments["procurement_review"] = StepAssignment.for_capability(
            "some.other.capability", unit_id
        )

    with pytest.raises(ToolError) as error:
        ApprovalEngine(ApprovalRepository()).start_instance(
            db,
            definition=PROCUREMENT_V1,
            subject_type="test_subject",
            applicant_user_id=uuid.uuid4(),
            organization_unit_id=unit_id,
            assignments=assignments,
            now=NOW,
        )

    assert error.value.code == "approval_assignment_mismatch"
    db.add.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


def test_repository_does_not_swallow_flush_failure_or_commit() -> None:
    db = MagicMock()
    db.flush.side_effect = RuntimeError("database rejected batch")
    unit_id = uuid.uuid4()

    with pytest.raises(RuntimeError, match="database rejected batch"):
        ApprovalEngine(ApprovalRepository()).start_instance(
            db,
            definition=PROCUREMENT_V1,
            subject_type="test_subject",
            applicant_user_id=uuid.uuid4(),
            organization_unit_id=unit_id,
            assignments=valid_assignments(uuid.uuid4(), unit_id),
            now=NOW,
        )

    db.commit.assert_not_called()
    db.rollback.assert_not_called()


def test_engine_uses_injected_repository_even_when_it_is_falsey() -> None:
    expected = (MagicMock(), (MagicMock(), MagicMock()))

    class FalseyRepository:
        def __init__(self) -> None:
            self.add_instance_with_tasks = MagicMock(return_value=expected)

        def __bool__(self) -> bool:
            return False

    repository = FalseyRepository()
    unit_id = uuid.uuid4()

    result = ApprovalEngine(repository).start_instance(  # type: ignore[arg-type]
        MagicMock(),
        definition=PROCUREMENT_V1,
        subject_type="test_subject",
        applicant_user_id=uuid.uuid4(),
        organization_unit_id=unit_id,
        assignments=valid_assignments(uuid.uuid4(), unit_id),
        now=NOW,
    )

    assert result is expected
    repository.add_instance_with_tasks.assert_called_once()


def _transition_fixture() -> tuple[
    ApprovalInstance, list[ApprovalTask], User, User
]:
    applicant = User(
        id=uuid.uuid4(),
        username="applicant",
        password_hash="hash",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )
    manager = User(
        id=uuid.uuid4(),
        username="manager",
        password_hash="hash",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )
    instance = ApprovalInstance(
        id=uuid.uuid4(),
        process_key="procurement.request",
        process_version=1,
        subject_type="procurement_request",
        applicant_user_id=applicant.id,
        organization_unit_id=uuid.uuid4(),
        status=ApprovalInstanceStatus.RUNNING,
        current_step_key="department_manager_review",
        version=1,
        submitted_at=NOW,
    )
    tasks = [
        ApprovalTask(
            id=uuid.uuid4(),
            instance_id=instance.id,
            sequence=1,
            step_key="department_manager_review",
            step_label="部门负责人审批",
            assignment_kind=AssignmentKind.USER,
            assigned_user_id=manager.id,
            status=ApprovalTaskStatus.PENDING,
            activated_at=NOW,
        ),
        ApprovalTask(
            id=uuid.uuid4(),
            instance_id=instance.id,
            sequence=2,
            step_key="procurement_review",
            step_label="采购专员复核",
            assignment_kind=AssignmentKind.CAPABILITY,
            required_capability="procurement.final.review",
            scope_organization_unit_id=instance.organization_unit_id,
            status=ApprovalTaskStatus.WAITING,
        ),
    ]
    return instance, tasks, applicant, manager


class TransitionRepository:
    def __init__(self, instance: ApprovalInstance, tasks: list[ApprovalTask]) -> None:
        self.instance = instance
        self.tasks = tasks
        self.operations: dict[tuple[uuid.UUID, uuid.UUID], ApprovalCommandOperation] = {}
        self.decisions: list[ApprovalDecision] = []
        self.calls: list[str] = []

    def resolve_task_instance_id(self, db, task_id):
        self.calls.append("resolve")
        task = next((item for item in self.tasks if item.id == task_id), None)
        return None if task is None else task.instance_id

    def lock_instance(self, db, instance_id):
        self.calls.append("lock_instance")
        return self.instance if self.instance.id == instance_id else None

    def resolve_instance_version(self, db, instance_id):
        self.calls.append("resolve_instance_version")
        return self.instance.version if self.instance.id == instance_id else None

    def get_command_operation(self, db, actor_user_id, client_operation_id):
        self.calls.append("get_operation")
        return self.operations.get((actor_user_id, client_operation_id))

    def claim_command_operation(self, db, **values):
        self.calls.append("claim_operation")
        key = (values["actor_user_id"], values["client_operation_id"])
        existing = self.operations.get(key)
        if existing is not None:
            return existing, False
        operation = ApprovalCommandOperation(id=uuid.uuid4(), **values)
        self.operations[key] = operation
        return operation, True

    def lock_task(self, db, instance_id, task_id):
        self.calls.append("lock_task")
        return next(
            (item for item in self.tasks if item.id == task_id and item.instance_id == instance_id),
            None,
        )

    def lock_open_tasks(self, db, instance_id):
        self.calls.append("lock_open_tasks")
        return tuple(
            item
            for item in self.tasks
            if item.instance_id == instance_id
            and item.status in (ApprovalTaskStatus.WAITING, ApprovalTaskStatus.PENDING)
        )

    def list_tasks(self, db, instance_id):
        self.calls.append("list_tasks")
        return tuple(item for item in self.tasks if item.instance_id == instance_id)

    def add_decision(self, db, decision):
        self.calls.append("add_decision")
        self.decisions.append(decision)

    def flush(self, db):
        self.calls.append("flush")


class RecordingAuthorization:
    def __init__(self, repository: TransitionRepository) -> None:
        self.repository = repository
        self.calls: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, ApprovalDecisionAction]] = []

    def authorize(self, *, instance, task, actor, action):
        assert "lock_task" in self.repository.calls
        assert "add_decision" not in self.repository.calls
        self.calls.append((instance.id, task.id, actor.id, action))


def test_manager_approve_locks_in_order_authorizes_before_write_and_activates_next() -> None:
    instance, tasks, _, manager = _transition_fixture()
    repository = TransitionRepository(instance, tasks)
    authorization = RecordingAuthorization(repository)
    operation_id = uuid.uuid4()

    result = ApprovalEngine(repository).approve_task(  # type: ignore[arg-type]
        MagicMock(),
        task_id=tasks[0].id,
        actor=manager,
        client_operation_id=operation_id,
        comment=" ok ",
        authorize=authorization,
        now=NOW,
    )

    assert repository.calls[:5] == [
        "resolve",
        "lock_instance",
        "get_operation",
        "claim_operation",
        "lock_task",
    ]
    assert authorization.calls == [
        (instance.id, tasks[0].id, manager.id, ApprovalDecisionAction.APPROVE)
    ]
    assert tasks[0].status is ApprovalTaskStatus.APPROVED
    assert tasks[1].status is ApprovalTaskStatus.PENDING
    assert instance.current_step_key == tasks[1].step_key
    assert instance.status is ApprovalInstanceStatus.RUNNING
    assert len(repository.decisions) == 1
    assert repository.decisions[0].comment == "ok"
    assert result.replayed is False
    assert result.operation.status is ApprovalCommandStatus.SUCCEEDED


def test_final_approve_completes_instance() -> None:
    instance, tasks, _, manager = _transition_fixture()
    tasks[0].status = ApprovalTaskStatus.APPROVED
    tasks[0].completed_at = NOW
    tasks[1].status = ApprovalTaskStatus.PENDING
    tasks[1].activated_at = NOW
    instance.current_step_key = tasks[1].step_key
    repository = TransitionRepository(instance, tasks)

    ApprovalEngine(repository).approve_task(  # type: ignore[arg-type]
        MagicMock(), task_id=tasks[1].id, actor=manager,
        client_operation_id=uuid.uuid4(), comment=None,
        authorize=RecordingAuthorization(repository), now=NOW,
    )

    assert instance.status is ApprovalInstanceStatus.APPROVED
    assert instance.current_step_key is None
    assert instance.completed_at == NOW


def test_reject_requires_nonblank_reason_before_claim() -> None:
    instance, tasks, _, manager = _transition_fixture()
    repository = TransitionRepository(instance, tasks)

    with pytest.raises(ToolError) as error:
        ApprovalEngine(repository).reject_task(  # type: ignore[arg-type]
            MagicMock(), task_id=tasks[0].id, actor=manager,
            client_operation_id=uuid.uuid4(), comment=" \t ",
            authorize=RecordingAuthorization(repository), now=NOW,
        )

    assert error.value.code == "approval_decision_reason_required"
    assert repository.calls == []


def test_reject_terminates_instance_and_cancels_unfinished_tasks() -> None:
    instance, tasks, _, manager = _transition_fixture()
    repository = TransitionRepository(instance, tasks)

    ApprovalEngine(repository).reject_task(  # type: ignore[arg-type]
        MagicMock(), task_id=tasks[0].id, actor=manager,
        client_operation_id=uuid.uuid4(), comment="不符合要求",
        authorize=RecordingAuthorization(repository), now=NOW,
    )

    assert instance.status is ApprovalInstanceStatus.REJECTED
    assert tasks[0].status is ApprovalTaskStatus.REJECTED
    assert tasks[1].status is ApprovalTaskStatus.CANCELLED
    assert repository.decisions[0].action is ApprovalDecisionAction.REJECT


def test_cancel_after_manager_approval_preserves_decision_and_cancels_open_task() -> None:
    instance, tasks, applicant, _ = _transition_fixture()
    tasks[0].status = ApprovalTaskStatus.APPROVED
    tasks[0].completed_at = NOW
    tasks[1].status = ApprovalTaskStatus.PENDING
    tasks[1].activated_at = NOW
    instance.current_step_key = tasks[1].step_key
    repository = TransitionRepository(instance, tasks)

    result = ApprovalEngine(repository).cancel_instance(  # type: ignore[arg-type]
        MagicMock(), instance_id=instance.id, actor_user_id=applicant.id,
        client_operation_id=uuid.uuid4(), now=NOW,
    )

    assert repository.calls[:4] == [
        "resolve_instance_version", "lock_instance", "get_operation", "claim_operation"
    ]
    assert result.instance.status is ApprovalInstanceStatus.CANCELLED
    assert tasks[0].status is ApprovalTaskStatus.APPROVED
    assert tasks[1].status is ApprovalTaskStatus.CANCELLED
    assert repository.decisions == []


def test_cancel_requires_locked_instance_owner() -> None:
    instance, tasks, _, manager = _transition_fixture()
    repository = TransitionRepository(instance, tasks)

    with pytest.raises(ToolError) as error:
        ApprovalEngine(repository).cancel_instance(  # type: ignore[arg-type]
            MagicMock(), instance_id=instance.id, actor_user_id=manager.id,
            client_operation_id=uuid.uuid4(), now=NOW,
        )

    assert error.value.code == "approval_task_not_assigned"
    assert all(task.status is not ApprovalTaskStatus.CANCELLED for task in tasks)


def test_same_operation_replays_after_terminal_without_new_decision() -> None:
    instance, tasks, _, manager = _transition_fixture()
    repository = TransitionRepository(instance, tasks)
    engine = ApprovalEngine(repository)  # type: ignore[arg-type]
    operation_id = uuid.uuid4()
    first = engine.reject_task(
        MagicMock(), task_id=tasks[0].id, actor=manager,
        client_operation_id=operation_id, comment="no",
        authorize=RecordingAuthorization(repository), now=NOW,
    )
    call_count = len(repository.calls)

    replay = engine.reject_task(
        MagicMock(), task_id=tasks[0].id, actor=manager,
        client_operation_id=operation_id, comment="no",
        authorize=RecordingAuthorization(repository), now=NOW,
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert len(repository.decisions) == 1
    assert "lock_task" not in repository.calls[call_count:]


def test_same_operation_with_different_payload_or_action_conflicts() -> None:
    instance, tasks, _, manager = _transition_fixture()
    repository = TransitionRepository(instance, tasks)
    engine = ApprovalEngine(repository)  # type: ignore[arg-type]
    operation_id = uuid.uuid4()
    engine.approve_task(
        MagicMock(), task_id=tasks[0].id, actor=manager,
        client_operation_id=operation_id, comment="first",
        authorize=RecordingAuthorization(repository), now=NOW,
    )

    with pytest.raises(ToolError) as error:
        engine.reject_task(
            MagicMock(), task_id=tasks[0].id, actor=manager,
            client_operation_id=operation_id, comment="different",
            authorize=RecordingAuthorization(repository), now=NOW,
        )

    assert error.value.code == "approval_operation_id_conflict"


def test_new_operation_against_terminal_instance_is_state_conflict() -> None:
    instance, tasks, applicant, _ = _transition_fixture()
    repository = TransitionRepository(instance, tasks)
    engine = ApprovalEngine(repository)  # type: ignore[arg-type]
    first_id = uuid.uuid4()
    first = engine.cancel_instance(
        MagicMock(), instance_id=instance.id, actor_user_id=applicant.id,
        client_operation_id=first_id, now=NOW,
    )
    replay = engine.cancel_instance(
        MagicMock(), instance_id=instance.id, actor_user_id=applicant.id,
        client_operation_id=first_id, now=NOW,
    )
    assert first.replayed is False and replay.replayed is True

    with pytest.raises(ToolError) as error:
        engine.cancel_instance(
            MagicMock(), instance_id=instance.id, actor_user_id=applicant.id,
            client_operation_id=uuid.uuid4(), now=NOW,
        )
    assert error.value.code == "approval_instance_state_conflict"
