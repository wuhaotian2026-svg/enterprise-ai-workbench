from __future__ import annotations

import uuid

import pytest

from policy_api.models import User, UserRole
from policy_api.workbench import catalog
from policy_api.workbench.capabilities import Capability
from policy_api.workbench.catalog import (
    DEFAULT_MODULES,
    ModuleCatalog,
    ModuleDefinition,
)
from policy_api.workbench.router import KNOWN_MODULE_KEYS


class AllowAllResolver:
    def has(self, _db: object, _user: User, _capability: Capability) -> bool:
        return True


class EmployeeResolver:
    def has(self, _db: object, _user: User, capability: Capability) -> bool:
        return capability in {
            Capability.KNOWLEDGE_ASK,
            Capability.HR_LEAVE_SELF_SERVICE,
        }


def user() -> User:
    return User(
        id=uuid.uuid4(),
        username="catalog-user",
        password_hash="hash",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )


def test_default_catalog_has_stable_server_order_and_metadata() -> None:
    modules = ModuleCatalog(AllowAllResolver()).allowed_modules(object(), user())

    assert [item.key for item in modules] == [
        "knowledge",
        "hr-assistant",
        "my-requests",
        "hr-review",
        "knowledge-admin",
        "organization",
        "analytics",
        "procurement",
        "approval-center",
    ]
    assert [item.index for item in modules] == [
        "01",
        "02",
        "03",
        "04",
        "05",
        "06",
        "07",
        "08",
        "09",
    ]
    assert [item.label for item in modules] == [
        "制度知识库",
        "请假助手",
        "我的申请",
        "HR 审核",
        "知识管理",
        "企业组织",
        "运营驾驶舱",
        "采购申请",
        "审批中心",
    ]

    definitions = {item.key: item for item in modules}
    assert (
        definitions["procurement"].capability
        == Capability.PROCUREMENT_REQUEST_SELF_SERVICE
    )
    assert (
        definitions["approval-center"].capability
        == Capability.APPROVAL_INBOX_VIEW
    )


def test_trusted_module_keys_are_derived_from_default_catalog() -> None:
    assert catalog.DEFAULT_MODULE_KEYS == frozenset(
        module.key for module in DEFAULT_MODULES
    )
    assert KNOWN_MODULE_KEYS is catalog.DEFAULT_MODULE_KEYS


def test_allowed_modules_removes_every_module_without_capability() -> None:
    modules = ModuleCatalog(EmployeeResolver()).allowed_modules(object(), user())

    assert [item.key for item in modules] == [
        "knowledge",
        "hr-assistant",
        "my-requests",
    ]


def test_module_definition_rejects_unknown_capability_at_construction() -> None:
    with pytest.raises(ValueError):
        ModuleDefinition(
            key="injected",
            label="Injected",
            index="99",
            capability="server.injected",  # type: ignore[arg-type]
        )
