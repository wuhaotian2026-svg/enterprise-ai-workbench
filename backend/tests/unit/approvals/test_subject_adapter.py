from __future__ import annotations

import ast
from datetime import date, datetime, timezone
from decimal import Decimal
import importlib
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from pydantic import ValidationError


def _task8_modules():
    try:
        schemas = importlib.import_module("policy_api.approvals.schemas")
        adapters = importlib.import_module("policy_api.approvals.subject_adapter")
    except ModuleNotFoundError as error:
        pytest.fail(f"Task 8 subject adapter is missing: {error}")
    return schemas, adapters


def _summary(schemas):
    return schemas.SubjectSummary(
        request_number="PR-20260824-0001",
        title="研发工作站",
        total=Decimal("24000.00"),
        status="pending_manager",
    )


def _detail(schemas):
    return schemas.SubjectDetail(
        purpose="研发环境升级",
        needed_by_date=date(2030, 2, 1),
        currency="CNY",
        items=(
            schemas.SubjectItem(
                category="it_equipment",
                name="开发工作站",
                specification="64 GB",
                quantity=Decimal("2.00"),
                unit="台",
                unit_price=Decimal("12000.00"),
                subtotal=Decimal("24000.00"),
            ),
        ),
        applicant=schemas.SubjectApplicant(display_name="Applicant"),
        organization=schemas.SubjectOrganization(display_name="Task 8 Unit"),
        timeline=(
            schemas.SubjectTimelineEntry(
                kind="submitted",
                occurred_at=datetime(2026, 8, 24, 2, 30, tzinfo=timezone.utc),
                step_key=None,
                step_label=None,
                action=None,
                actor_display_name="Applicant",
                comment=None,
                status="pending_manager",
            ),
        ),
    )


class _Adapter:
    subject_type = "example_subject"

    def __init__(self, schemas) -> None:
        self._schemas = schemas

    def summary(self, db, instance):
        return _summary(self._schemas)

    def detail(self, db, instance):
        return _detail(self._schemas)


class _RawAdapter:
    subject_type = "raw_subject"

    def summary(self, db, instance):
        return {"secret": "raw ORM-like data"}

    def detail(self, db, instance):
        return {"secret": "raw ORM-like data"}


def test_closed_subject_dtos_are_frozen_and_forbid_extra_fields() -> None:
    schemas, _ = _task8_modules()
    summary = _summary(schemas)
    detail = _detail(schemas)

    assert summary.model_dump() == {
        "request_number": "PR-20260824-0001",
        "title": "研发工作站",
        "total": Decimal("24000.00"),
        "status": "pending_manager",
    }
    assert detail.items[0].name == "开发工作站"
    assert detail.organization.display_name == "Task 8 Unit"
    with pytest.raises(ValidationError):
        schemas.SubjectSummary.model_validate(
            {
                **summary.model_dump(),
                "approval_instance_id": str(uuid.uuid4()),
            }
        )
    with pytest.raises(ValidationError):
        schemas.SubjectDetail.model_validate(
            {
                **detail.model_dump(),
                "internal_row": {"password_hash": "must-not-leak"},
            }
        )
    with pytest.raises(ValidationError):
        schemas.SubjectItem.model_validate(
            {**detail.items[0].model_dump(), "vendor_bank_account": "hidden"}
        )
    with pytest.raises(ValidationError):
        summary.title = "mutated"


def test_registry_defensively_snapshots_adapters_and_rejects_duplicates() -> None:
    schemas, adapters = _task8_modules()
    adapter = _Adapter(schemas)
    supplied = [adapter]
    registry = adapters.SubjectAdapterRegistry(supplied)
    supplied.clear()

    assert registry.require("example_subject") is adapter
    assert registry.summary("example_subject", object(), object()) == _summary(schemas)
    assert registry.detail("example_subject", object(), object()) == _detail(schemas)
    with pytest.raises(adapters.ApprovalError) as duplicate:
        adapters.SubjectAdapterRegistry((adapter, _Adapter(schemas)))
    assert duplicate.value.code == "approval_subject_duplicate"


def test_registry_fails_closed_for_unknown_or_raw_adapter_results() -> None:
    _, adapters = _task8_modules()
    registry = adapters.SubjectAdapterRegistry((_RawAdapter(),))

    with pytest.raises(adapters.ApprovalError) as unknown:
        registry.require("unknown")
    assert unknown.value.code == "approval_subject_not_supported"
    with pytest.raises(adapters.ApprovalError) as raw_summary:
        registry.summary("raw_subject", object(), object())
    assert raw_summary.value.code == "approval_subject_invalid"
    with pytest.raises(adapters.ApprovalError) as raw_detail:
        registry.detail("raw_subject", object(), object())
    assert raw_detail.value.code == "approval_subject_invalid"


def test_generic_approval_modules_have_static_and_dynamic_domain_boundaries() -> None:
    _task8_modules()
    approvals_dir = Path(__file__).parents[3] / "src" / "policy_api" / "approvals"
    forbidden_prefixes = ("policy_api.procurement", "policy_api.hr")
    for source_path in approvals_dir.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), source_path.name)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any(
            name.startswith(forbidden_prefixes) for name in imported
        ), f"domain import leaked into {source_path.name}: {imported}"

    backend_dir = approvals_dir.parents[2]
    script = (
        "import json,sys; "
        "import policy_api.approvals.schemas; "
        "import policy_api.approvals.subject_adapter; "
        "import policy_api.approvals.runtime; "
        "print(json.dumps(sorted(name for name in sys.modules "
        "if name.startswith(('policy_api.procurement','policy_api.hr')))))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_dir,
        env={
            **os.environ,
            "PYTHONPATH": str(backend_dir / "src"),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "[]"
