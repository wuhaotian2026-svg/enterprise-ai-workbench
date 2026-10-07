from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal, getcontext
import re
from types import SimpleNamespace
import uuid
from unittest.mock import ANY, MagicMock

import pytest

from policy_api.approvals.enums import ApprovalInstanceStatus
from policy_api.approvals.subject_adapter import ApprovalError
from policy_api.procurement.enums import (
    ProcurementCategoryCode,
    ProcurementCurrencyCode,
)
from policy_api.procurement.schemas import (
    ProcurementRequestInput,
    ProcurementRequestItemInput,
)
from policy_api.procurement.service import (
    PROCUREMENT_REQUEST_V1,
    SUBJECT_TYPE,
    ProcurementItemCommand,
    ProcurementService,
    ProcurementSubjectAdapter,
    SubmitProcurementRequestCommand,
    amount_bucket,
    canonical_submission_hash,
    item_count_bucket,
)
from policy_api.tools.errors import ToolError
from policy_api.models import User
from sqlalchemy.orm import Session


def _subject_adapter_fixture():
    instance = SimpleNamespace(
        id=uuid.uuid4(),
        subject_type=SUBJECT_TYPE,
        process_key=PROCUREMENT_REQUEST_V1.process_key,
        process_version=PROCUREMENT_REQUEST_V1.version,
        applicant_user_id=uuid.uuid4(),
        organization_unit_id=uuid.uuid4(),
        status=ApprovalInstanceStatus.RUNNING,
        current_step_key="department_manager_review",
        submitted_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        completed_at=None,
    )
    request = SimpleNamespace(
        id=uuid.uuid4(),
        request_number="PR-2030-000001",
        approval_instance_id=instance.id,
        organization_unit_id=instance.organization_unit_id,
        title="研发设备采购",
        total_amount=Decimal("12345.60"),
        purpose="研发使用",
        needed_by_date=date(2030, 1, 2),
        currency="CNY",
    )
    repository = MagicMock()
    repository.get_request_by_instance.return_value = request
    repository.get_subject_identity.return_value = ("Applicant", "Task 15 Unit")
    repository.list_items.return_value = ()
    repository.list_decision_facts.return_value = ()
    return ProcurementSubjectAdapter(repository=repository), repository, instance


def test_subject_detail_uses_one_instance_scoped_identity_query() -> None:
    adapter, repository, instance = _subject_adapter_fixture()

    detail = adapter.detail(MagicMock(spec=Session), instance)

    repository.get_request_by_instance.assert_called_once_with(ANY, instance.id)
    repository.get_subject_identity.assert_called_once_with(
        ANY,
        applicant_user_id=instance.applicant_user_id,
        organization_unit_id=instance.organization_unit_id,
    )
    assert detail.applicant.display_name == "Applicant"
    assert detail.organization.display_name == "Task 15 Unit"


def test_subject_detail_fails_closed_when_identity_context_is_missing() -> None:
    adapter, repository, instance = _subject_adapter_fixture()
    repository.get_subject_identity.return_value = None

    with pytest.raises(ApprovalError, match="approval_subject_invalid"):
        adapter.detail(MagicMock(spec=Session), instance)


def command(
    *,
    items: tuple[ProcurementItemCommand, ...] | None = None,
    title: str = "研发设备采购",
) -> SubmitProcurementRequestCommand:
    return SubmitProcurementRequestCommand(
        title=title,
        purpose="用于研发测试 unicode：处理器 🚀",
        needed_by_date=date(2030, 1, 2),
        currency=ProcurementCurrencyCode.CNY,
        items=items
        or (
            ProcurementItemCommand(
                category_code=ProcurementCategoryCode.IT_EQUIPMENT,
                item_name="便携式工作站",
                specification="32 GB",
                quantity=Decimal("1.00"),
                unit="台",
                estimated_unit_price=Decimal("12345.60"),
            ),
        ),
    )


def test_command_types_are_frozen_and_slotted() -> None:
    item = command().items[0]
    with pytest.raises(FrozenInstanceError):
        item.item_name = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        command().title = "changed"  # type: ignore[misc]
    assert not hasattr(item, "__dict__")
    assert not hasattr(command(), "__dict__")


@pytest.mark.parametrize(
    ("left_quantity", "left_price", "right_quantity", "right_price"),
    [
        ("1", "2", "1.0", "2.00"),
        ("1.00", "0", "1", "-0.00"),
        ("1000.000", "0.0100", "1E+3", "1E-2"),
    ],
)
def test_canonical_hash_normalizes_equivalent_decimals_and_signed_zero(
    left_quantity: str,
    left_price: str,
    right_quantity: str,
    right_price: str,
) -> None:
    def item(quantity: str, price: str) -> ProcurementItemCommand:
        return ProcurementItemCommand(
            category_code=ProcurementCategoryCode.OTHER,
            item_name="测试",
            specification=None,
            quantity=Decimal(quantity),
            unit="件",
            estimated_unit_price=Decimal(price),
        )

    assert canonical_submission_hash(command(items=(item(left_quantity, left_price),))) == (
        canonical_submission_hash(command(items=(item(right_quantity, right_price),)))
    )


def test_canonical_hash_is_lowercase_sha256_stable_across_ambient_decimal_context() -> None:
    original_precision = getcontext().prec
    try:
        getcontext().prec = 3
        first = canonical_submission_hash(command())
        getcontext().prec = 50
        second = canonical_submission_hash(command())
    finally:
        getcontext().prec = original_precision

    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def test_canonical_hash_preserves_unicode_and_ordered_items() -> None:
    first = command().items[0]
    second = ProcurementItemCommand(
        category_code=ProcurementCategoryCode.SOFTWARE_SERVICE,
        item_name="许可证",
        specification="年度订阅",
        quantity=Decimal("2"),
        unit="套",
        estimated_unit_price=Decimal("500"),
    )

    assert canonical_submission_hash(command(items=(first, second))) != (
        canonical_submission_hash(command(items=(second, first)))
    )
    assert canonical_submission_hash(command(title="研发设备采购")) != (
        canonical_submission_hash(command(title="研发设备采购é"))
    )


def test_canonical_hash_api_excludes_actor_and_all_derived_identity_fields() -> None:
    signature = __import__("inspect").signature(canonical_submission_hash)
    assert tuple(signature.parameters) == ("command",)
    command_fields = set(SubmitProcurementRequestCommand.__dataclass_fields__)
    item_fields = set(ProcurementItemCommand.__dataclass_fields__)
    assert command_fields == {"title", "purpose", "needed_by_date", "currency", "items"}
    assert item_fields == {
        "category_code",
        "item_name",
        "specification",
        "quantity",
        "unit",
        "estimated_unit_price",
    }


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "1"), (2, "2_5"), (5, "2_5"), (6, "6_10"), (10, "6_10"), (11, "11_plus"), (50, "11_plus")],
)
def test_item_count_bucket_boundaries(count: int, expected: str) -> None:
    assert item_count_bucket(count) == expected


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("0", "0_999"),
        ("999.99", "0_999"),
        ("1000", "1000_9999"),
        ("9999.99", "1000_9999"),
        ("10000", "10000_99999"),
        ("99999.99", "10000_99999"),
        ("100000", "100000_plus"),
        ("999999999999.99", "100000_plus"),
    ],
)
def test_amount_bucket_decimal_boundaries(amount: str, expected: str) -> None:
    assert amount_bucket(Decimal(amount)) == expected


@pytest.mark.parametrize("count", [0, -1, 51])
def test_item_count_bucket_rejects_out_of_contract_values(count: int) -> None:
    with pytest.raises(ValueError, match="item_count_invalid"):
        item_count_bucket(count)


@pytest.mark.parametrize("amount", [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")])
def test_amount_bucket_rejects_invalid_values(amount: Decimal) -> None:
    with pytest.raises(ValueError, match="amount_invalid"):
        amount_bucket(amount)


def valid_item(**overrides: object) -> ProcurementItemCommand:
    values: dict[str, object] = {
        "category_code": ProcurementCategoryCode.OTHER,
        "item_name": " 合法项目 ",
        "specification": " 规格 Ω ",
        "quantity": Decimal("1.00"),
        "unit": " 件 ",
        "estimated_unit_price": Decimal("2.50"),
    }
    values.update(overrides)
    return ProcurementItemCommand(**values)  # type: ignore[arg-type]


def test_command_defensively_copies_external_items_and_preserves_hash_snapshot() -> None:
    external_items = [valid_item()]
    snapshot = SubmitProcurementRequestCommand(
        title=" 合法标题 ",
        purpose=" 合法用途 🚀 ",
        needed_by_date=date(2030, 1, 2),
        currency=ProcurementCurrencyCode.CNY,
        items=external_items,  # type: ignore[arg-type]
    )
    original_hash = canonical_submission_hash(snapshot)

    external_items.append(valid_item(item_name="后来加入"))

    assert isinstance(snapshot.items, tuple)
    assert len(snapshot.items) == 1
    assert canonical_submission_hash(snapshot) == original_hash
    assert snapshot.title == "合法标题"
    assert snapshot.purpose == "合法用途 🚀"
    assert snapshot.items[0].item_name == "合法项目"
    assert snapshot.items[0].specification == "规格 Ω"
    assert snapshot.items[0].unit == "件"


def test_command_snapshot_matches_task6_text_contract_and_has_stable_hash() -> None:
    title = " \t审批\n标题\u200d \r\n"
    purpose = " \n采购\t用途\u200c \t"
    item_name = " \t项目\n名称\u200d "
    specification = " \n规格\t说明\u200c "
    unit = " \t件\n箱\u200d "
    schema_result = ProcurementRequestInput.model_validate(
        {
            "title": title,
            "purpose": purpose,
            "needed_by_date": date(2030, 1, 2),
            "currency": ProcurementCurrencyCode.CNY,
            "items": [
                {
                    "category_code": ProcurementCategoryCode.OTHER,
                    "item_name": item_name,
                    "specification": specification,
                    "quantity": Decimal("1.00"),
                    "unit": unit,
                    "estimated_unit_price": Decimal("2.50"),
                }
            ],
        },
        context={"today": date(2030, 1, 1)},
    )

    submitted = SubmitProcurementRequestCommand(
        title=title,
        purpose=purpose,
        needed_by_date=date(2030, 1, 2),
        currency=ProcurementCurrencyCode.CNY,
        items=(
            ProcurementItemCommand(
                category_code=ProcurementCategoryCode.OTHER,
                item_name=item_name,
                specification=specification,
                quantity=Decimal("1.00"),
                unit=unit,
                estimated_unit_price=Decimal("2.50"),
            ),
        ),
    )
    snapshot = ProcurementService._submission_snapshot(submitted)

    assert submitted.title == snapshot.title == schema_result.title
    assert submitted.purpose == snapshot.purpose == schema_result.purpose
    assert snapshot.items[0] is not submitted.items[0]
    assert snapshot.items[0].item_name == schema_result.items[0].item_name
    assert snapshot.items[0].specification == schema_result.items[0].specification
    assert snapshot.items[0].unit == schema_result.items[0].unit
    assert canonical_submission_hash(snapshot) == canonical_submission_hash(submitted)


def test_submission_snapshot_deep_copies_and_normalizes_tampered_nested_item() -> None:
    submitted = command()
    source_item = submitted.items[0]
    object.__setattr__(source_item, "category_code", "software_service")
    object.__setattr__(source_item, "item_name", " 规范化项目 ")
    object.__setattr__(source_item, "specification", " 订阅规格 ")
    object.__setattr__(source_item, "quantity", "2.00")
    object.__setattr__(source_item, "unit", " 套 ")
    object.__setattr__(source_item, "estimated_unit_price", "3.50")

    snapshot = ProcurementService._submission_snapshot(submitted)

    normalized = snapshot.items[0]
    assert normalized is not source_item
    assert normalized.category_code is ProcurementCategoryCode.SOFTWARE_SERVICE
    assert normalized.item_name == "规范化项目"
    assert normalized.specification == "订阅规格"
    assert normalized.quantity == Decimal("2.00")
    assert normalized.unit == "套"
    assert normalized.estimated_unit_price == Decimal("3.50")
    snapshot_hash = canonical_submission_hash(snapshot)

    object.__setattr__(source_item, "quantity", "9.00")

    assert canonical_submission_hash(snapshot) == snapshot_hash
    assert normalized.quantity == Decimal("2.00")
    assert source_item.quantity == "9.00"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("category_code", object(), "procurement_item_invalid"),
        ("item_name", object(), "procurement_item_invalid"),
        ("quantity", Decimal("1e1000000"), "procurement_amount_invalid"),
        ("estimated_unit_price", "2.50", "procurement_amount_invalid"),
    ],
)
def test_from_normalized_revalidates_mutated_pydantic_item(
    field: str, value: object, code: str
) -> None:
    normalized = ProcurementRequestItemInput.model_validate(
        {
            "category_code": ProcurementCategoryCode.OTHER,
            "item_name": "合法项目",
            "specification": None,
            "quantity": Decimal("1.00"),
            "unit": "件",
            "estimated_unit_price": Decimal("2.50"),
        }
    )
    object.__setattr__(normalized, field, value)

    with pytest.raises(ValueError, match=code):
        ProcurementItemCommand._from_normalized(normalized)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category_code", "not-a-category"),
        ("item_name", "   "),
        ("quantity", Decimal("NaN")),
        ("estimated_unit_price", object()),
    ],
)
def test_submission_snapshot_maps_illegal_nested_tampering_to_stable_error(
    field: str, value: object
) -> None:
    submitted = command()
    object.__setattr__(submitted.items[0], field, value)

    with pytest.raises(ToolError, match="procurement_request_invalid"):
        ProcurementService._submission_snapshot(submitted)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("item_name", "bad\ud800text", "procurement_item_invalid"),
        ("title", "bad\ud800text", "procurement_request_invalid"),
    ],
)
def test_command_rejects_only_non_utf8_text_with_stable_error(
    field: str, value: str, code: str
) -> None:
    with pytest.raises(ValueError, match=code):
        if field == "item_name":
            valid_item(item_name=value)
        else:
            command(title=value)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"category_code": "other"}, "procurement_item_invalid"),
        ({"item_name": "   "}, "procurement_item_invalid"),
        ({"item_name": "x" * 201}, "procurement_item_invalid"),
        ({"specification": "x" * 501}, "procurement_item_invalid"),
        ({"unit": "   "}, "procurement_item_invalid"),
        ({"unit": "x" * 41}, "procurement_item_invalid"),
        ({"quantity": 1.0}, "procurement_amount_invalid"),
        ({"quantity": Decimal("NaN")}, "procurement_amount_invalid"),
        ({"quantity": Decimal("Infinity")}, "procurement_amount_invalid"),
        ({"quantity": Decimal("-1")}, "procurement_amount_invalid"),
        ({"quantity": Decimal("1.001")}, "procurement_amount_invalid"),
        ({"quantity": Decimal("12345678901.00")}, "procurement_amount_invalid"),
        ({"estimated_unit_price": 2.5}, "procurement_amount_invalid"),
        ({"estimated_unit_price": Decimal("NaN")}, "procurement_amount_invalid"),
        ({"estimated_unit_price": Decimal("-0.01")}, "procurement_amount_invalid"),
        ({"estimated_unit_price": Decimal("1.001")}, "procurement_amount_invalid"),
        ({"estimated_unit_price": Decimal("1234567890123.00")}, "procurement_amount_invalid"),
        ({"estimated_unit_price": Decimal("1e1000000")}, "procurement_amount_invalid"),
    ],
)
def test_item_command_rejects_invalid_direct_construction_before_hashing(
    overrides: dict[str, object], code: str
) -> None:
    with pytest.raises(ValueError, match=code):
        valid_item(**overrides)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"title": "   "}, "procurement_request_invalid"),
        ({"title": "x" * 161}, "procurement_request_invalid"),
        ({"purpose": "   "}, "procurement_request_invalid"),
        ({"purpose": "x" * 2001}, "procurement_request_invalid"),
        ({"needed_by_date": datetime(2030, 1, 2)}, "procurement_needed_date_invalid"),
        ({"currency": "CNY"}, "procurement_request_invalid"),
        ({"items": []}, "procurement_items_required"),
        ({"items": [valid_item()] * 51}, "procurement_items_required"),
        ({"items": [object()]}, "procurement_item_invalid"),
    ],
)
def test_request_command_rejects_invalid_direct_construction(
    overrides: dict[str, object], code: str
) -> None:
    values: dict[str, object] = {
        "title": "标题",
        "purpose": "用途",
        "needed_by_date": date(2030, 1, 2),
        "currency": ProcurementCurrencyCode.CNY,
        "items": [valid_item()],
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=code):
        SubmitProcurementRequestCommand(**values)  # type: ignore[arg-type]


def test_successful_commit_returns_flushed_result_without_refresh_or_rollback() -> None:
    db = MagicMock(spec=Session)
    actor = MagicMock(spec=User)
    actor.id = uuid.uuid4()
    result = MagicMock()
    service = ProcurementService(now_factory=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    service._stage_submission = MagicMock(return_value=result)  # type: ignore[method-assign]

    returned = service.submit_procurement_request(
        db,
        actor=actor,
        client_operation_id=uuid.uuid4(),
        command=command(),
        request_id="trace",
    )

    assert returned is result
    db.commit.assert_called_once_with()
    db.rollback.assert_not_called()
    db.refresh.assert_not_called()


def test_tampered_command_is_mapped_to_stable_error_before_database_access() -> None:
    db = MagicMock(spec=Session)
    actor = MagicMock(spec=User)
    actor.id = uuid.uuid4()
    tampered = command()
    object.__setattr__(tampered, "items", (object(),))

    with pytest.raises(Exception) as error:
        ProcurementService(
            now_factory=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
        ).submit_procurement_request(
            db,
            actor=actor,
            client_operation_id=uuid.uuid4(),
            command=tampered,
            request_id="trace",
        )

    assert getattr(error.value, "code", None) == "procurement_item_invalid"
    db.scalar.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_called_once_with()


@pytest.mark.parametrize("failure_point", ["stage", "commit"])
def test_stage_or_commit_failure_rolls_back_exactly_once(
    failure_point: str,
) -> None:
    db = MagicMock(spec=Session)
    actor = MagicMock(spec=User)
    actor.id = uuid.uuid4()
    result = MagicMock()
    service = ProcurementService(
        now_factory=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
    )
    if failure_point == "stage":
        service._stage_submission = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("stage failed")
        )
    else:
        service._stage_submission = MagicMock(  # type: ignore[method-assign]
            return_value=result
        )
        db.commit.side_effect = RuntimeError("commit failed")

    with pytest.raises(RuntimeError, match=f"{failure_point} failed"):
        service.submit_procurement_request(
            db,
            actor=actor,
            client_operation_id=uuid.uuid4(),
            command=command(),
            request_id="trace",
        )

    assert db.commit.call_count == (0 if failure_point == "stage" else 1)
    db.rollback.assert_called_once_with()
    db.refresh.assert_not_called()


def test_commit_false_success_does_not_commit_rollback_or_refresh() -> None:
    db = MagicMock(spec=Session)
    actor = MagicMock(spec=User)
    actor.id = uuid.uuid4()
    result = MagicMock()
    service = ProcurementService(
        now_factory=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
    )
    service._stage_submission = MagicMock(return_value=result)  # type: ignore[method-assign]

    returned = service.submit_procurement_request(
        db,
        actor=actor,
        client_operation_id=uuid.uuid4(),
        command=command(),
        request_id="trace",
        commit=False,
    )

    assert returned is result
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    db.refresh.assert_not_called()


def test_preflight_submit_reuses_authoritative_checks_without_side_effects() -> None:
    db = MagicMock(spec=Session)
    actor = MagicMock(spec=User)
    actor.id = uuid.uuid4()
    authoritative = MagicMock(spec=User)
    authoritative.id = actor.id
    profile = MagicMock()
    profile.organization_unit_id = uuid.uuid4()
    profile.manager_employee_id = uuid.uuid4()
    organization = MagicMock()
    organization.id = profile.organization_unit_id
    manager_profile = MagicMock()
    manager_profile.user_id = uuid.uuid4()
    manager = MagicMock(spec=User)
    manager.id = manager_profile.user_id
    repository = MagicMock()
    repository.get_active_user.side_effect = [authoritative, manager]
    repository.get_active_profile_by_user.return_value = profile
    repository.get_active_organization.return_value = organization
    repository.get_active_employee.return_value = manager_profile
    applicant_scope = SimpleNamespace(is_global=True, organization_unit_ids=frozenset())
    manager_scope = SimpleNamespace(is_global=True, organization_unit_ids=frozenset())
    resolver = MagicMock()
    resolver.scope_for.side_effect = [applicant_scope, manager_scope]
    service = ProcurementService(
        repository=repository,
        capability_resolver=resolver,
    )

    result = service.preflight_submit(db, actor=actor)

    assert result.authoritative_actor is authoritative
    assert result.profile is profile
    assert result.organization is organization
    assert result.manager is manager
    assert resolver.scope_for.call_count == 2
    db.add.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        ("inactive_employee", "procurement_profile_required"),
        ("no_self_service", "procurement_profile_required"),
        ("no_manager", "procurement_manager_unavailable"),
        ("manager_no_review_scope", "procurement_manager_capability_required"),
        ("manager_wrong_scope", "procurement_manager_capability_required"),
    ),
)
def test_preflight_submit_fails_closed_without_side_effects(
    mutation: str, code: str,
) -> None:
    db = MagicMock(spec=Session)
    actor = MagicMock(spec=User)
    actor.id = uuid.uuid4()
    authoritative = MagicMock(spec=User)
    authoritative.id = actor.id
    profile = MagicMock()
    profile.organization_unit_id = uuid.uuid4()
    profile.manager_employee_id = uuid.uuid4()
    organization = MagicMock()
    organization.id = profile.organization_unit_id
    manager_profile = MagicMock()
    manager_profile.user_id = uuid.uuid4()
    manager = MagicMock(spec=User)
    repository = MagicMock()
    repository.get_active_user.side_effect = [
        None if mutation == "inactive_employee" else authoritative,
        manager,
    ]
    repository.get_active_profile_by_user.return_value = profile
    repository.get_active_organization.return_value = organization
    repository.get_active_employee.return_value = (
        None if mutation == "no_manager" else manager_profile
    )
    applicant_scope = SimpleNamespace(is_global=True, organization_unit_ids=frozenset())
    manager_scope = SimpleNamespace(
        is_global=mutation != "manager_wrong_scope",
        organization_unit_ids=(
            frozenset({uuid.uuid4()})
            if mutation == "manager_wrong_scope" else frozenset()
        ),
    )
    resolver = MagicMock()
    resolver.scope_for.side_effect = [
        None if mutation == "no_self_service" else applicant_scope,
        None if mutation == "manager_no_review_scope" else manager_scope,
    ]
    service = ProcurementService(repository=repository, capability_resolver=resolver)

    with pytest.raises(ToolError, match=code):
        service.preflight_submit(db, actor=actor)

    db.add.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
