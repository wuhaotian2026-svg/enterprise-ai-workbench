from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from policy_api.database import assert_test_database_url
from policy_api.hr.enums import LeaveRequestStatus, LeaveTypeCode, WorkCalendarDayKind
from policy_api.hr.models import (
    EmployeeProfile,
    LeaveAccount,
    LeaveRequest,
    LeaveType,
    WorkCalendarDay,
)
from policy_api.hr.tools import (
    build_hr_tool_definitions,
    propose_hr_write,
)
from policy_api.knowledge.tools import (
    PolicyCitation,
    PolicySearchOutcome,
    build_knowledge_tool_definition,
)
from policy_api.models import User, UserRole
from policy_api.tools.definitions import ToolContext
from policy_api.tools.errors import ToolError
from policy_api.tools.executor import ToolExecutor
from policy_api.tools.registry import ToolRegistry
from policy_api.tools.schemas import ConfirmationBlock


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
EXPECTED_TOOLS = {
    "knowledge.search_policy",
    "hr.get_my_leave_balances",
    "hr.calculate_leave_duration",
    "hr.list_my_leave_requests",
    "hr.get_my_leave_request",
    "hr.submit_leave_request",
    "hr.cancel_leave_request",
}


@pytest.fixture
def hr_tools_session() -> tuple[Session, dict[str, object]]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for HR tools tests")
    assert_test_database_url(TEST_DATABASE_URL)
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    original_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    command.upgrade(config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    start = date(2032, 1, 5)
    try:
        employee_user = User(
            username=f"tool-employee-{suffix}",
            password_hash="hash",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
        hr_user = User(
            username=f"tool-hr-{suffix}",
            password_hash="hash",
            role=UserRole.HR,
            is_active=True,
        )
        admin_user = User(
            username=f"tool-admin-{suffix}",
            password_hash="hash",
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add_all([employee_user, hr_user, admin_user])
        db.flush()
        employee = EmployeeProfile(
            user_id=employee_user.id,
            employee_number=f"E-{suffix}",
            display_name="Tool Employee",
            hire_date=date(2024, 1, 1),
            is_active=True,
        )
        annual = LeaveType(
            code=LeaveTypeCode.ANNUAL,
            display_name="Annual leave",
            is_enabled=True,
        )
        db.add_all([employee, annual])
        db.flush()
        account = LeaveAccount(
            employee_id=employee.id,
            leave_type_id=annual.id,
            year=start.year,
            entitled=Decimal("1.00"),
            used=Decimal("0.00"),
            reserved=Decimal("0.00"),
            version=1,
        )
        request = LeaveRequest(
            request_number=f"LR-{suffix}",
            employee_id=employee.id,
            leave_type_id=annual.id,
            start_date=start,
            end_date=start,
            workday_count=Decimal("1.00"),
            reason="pending",
            status=LeaveRequestStatus.PENDING,
            submitted_at=datetime.now(timezone.utc),
        )
        db.add_all([account, request])
        db.add_all(
            WorkCalendarDay(
                calendar_date=start + timedelta(days=offset),
                kind=WorkCalendarDayKind.WORKDAY,
                is_workday=True,
                label=None,
            )
            for offset in range(3)
        )
        db.flush()
        yield db, {
            "employee_user": employee_user,
            "hr_user": hr_user,
            "admin_user": admin_user,
            "account": account,
            "request": request,
            "start": start,
        }
    finally:
        db.close()
        transaction.rollback()
        connection.close()
        engine.dispose()
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url


def knowledge_search(_query: str) -> PolicySearchOutcome:
    return PolicySearchOutcome(
        status="answered",
        text="年假需要提前申请。",
        refusal_reason=None,
        citations=(
            PolicyCitation(
                number=1,
                chunk_id="chunk-1",
                document_name="员工休假制度.pdf",
                page_number=3,
                evidence_snapshot="员工应提前提交年假申请。",
            ),
        ),
    )


def test_exact_allowlist_role_visibility_and_no_actor_schema(
    hr_tools_session: tuple[Session, dict[str, object]],
) -> None:
    db, _items = hr_tools_session
    definitions = (
        build_knowledge_tool_definition(knowledge_search),
        *build_hr_tool_definitions(db),
    )
    assert {definition.name for definition in definitions} == EXPECTED_TOOLS
    assert all("employee_id" not in definition.input_model.model_fields for definition in definitions)
    registry = ToolRegistry(definitions)

    employee_names = {
        item["function"]["name"]
        for item in registry.provider_tools(role=UserRole.EMPLOYEE)
    }
    hr_names = {
        item["function"]["name"] for item in registry.provider_tools(role=UserRole.HR)
    }
    admin_names = {
        item["function"]["name"] for item in registry.provider_tools(role=UserRole.ADMIN)
    }
    assert len(employee_names) == len(EXPECTED_TOOLS)
    assert hr_names == employee_names
    assert admin_names == {"knowledge_search_policy"}


def test_read_tools_query_fresh_database_and_policy_keeps_citations(
    hr_tools_session: tuple[Session, dict[str, object]],
) -> None:
    db, items = hr_tools_session
    definitions = (
        build_knowledge_tool_definition(knowledge_search),
        *build_hr_tool_definitions(db),
    )
    registry = ToolRegistry(definitions)
    executor = ToolExecutor(registry)
    context = ToolContext(
        actor_user_id=items["employee_user"].id,
        role=UserRole.EMPLOYEE,
    )

    first = executor.execute(
        "hr.get_my_leave_balances", {"year": 2032}, context
    ).result
    items["account"].used = Decimal("0.50")
    db.flush()
    second = executor.execute(
        "hr.get_my_leave_balances", {"year": 2032}, context
    ).result
    assert first["model_result"]["balances"][0]["available"] == "1.00"
    assert second["model_result"]["balances"][0]["available"] == "0.50"

    policy = executor.execute(
        "knowledge.search_policy", {"query": "年假怎么申请"}, context
    ).result
    assert policy["blocks"][1]["type"] == "policy_citations"
    assert policy["blocks"][1]["citations"][0]["document_name"] == "员工休假制度.pdf"


def test_write_preview_rejects_insufficient_balance_and_cancel_requires_confirmation(
    hr_tools_session: tuple[Session, dict[str, object]],
) -> None:
    db, items = hr_tools_session
    definitions = {item.name: item for item in build_hr_tool_definitions(db)}
    context = ToolContext(
        actor_user_id=items["employee_user"].id,
        role=UserRole.EMPLOYEE,
    )
    persisted: list[tuple[str, dict[str, object], dict[str, object]]] = []

    def persist(
        tool_name: str,
        arguments: dict[str, object],
        preview: dict[str, object],
        _context: ToolContext,
    ) -> ConfirmationBlock:
        persisted.append((tool_name, arguments, preview))
        return ConfirmationBlock(
            confirmation_id=uuid.uuid4(),
            tool_name=tool_name,
            preview=preview,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )

    start = items["start"]
    with pytest.raises(ToolError, match="leave_balance_insufficient"):
        propose_hr_write(
            db,
            definitions["hr.submit_leave_request"],
            {
                "leave_type_code": "annual",
                "start_date": start.isoformat(),
                "end_date": (start + timedelta(days=1)).isoformat(),
                "reason": "不足余额",
            },
            context,
            persist=persist,
        )
    assert persisted == []

    block = propose_hr_write(
        db,
        definitions["hr.cancel_leave_request"],
        {"request_id": str(items["request"].id)},
        context,
        persist=persist,
    )
    assert block.type == "confirmation"
    assert persisted[0][0] == "hr.cancel_leave_request"
    assert items["request"].status == LeaveRequestStatus.PENDING
