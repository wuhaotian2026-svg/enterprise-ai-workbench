from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from policy_api.models import User
from policy_api.workbench.capabilities import Capability, CapabilityResolver


@dataclass(frozen=True, slots=True)
class ModuleDefinition:
    key: str
    label: str
    index: str
    capability: Capability

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", Capability(self.capability))


DEFAULT_MODULES = (
    ModuleDefinition(
        key="knowledge",
        label="制度知识库",
        index="01",
        capability=Capability.KNOWLEDGE_ASK,
    ),
    ModuleDefinition(
        key="hr-assistant",
        label="请假助手",
        index="02",
        capability=Capability.HR_LEAVE_SELF_SERVICE,
    ),
    ModuleDefinition(
        key="my-requests",
        label="我的申请",
        index="03",
        capability=Capability.HR_LEAVE_SELF_SERVICE,
    ),
    ModuleDefinition(
        key="hr-review",
        label="HR 审核",
        index="04",
        capability=Capability.HR_LEAVE_REVIEW,
    ),
    ModuleDefinition(
        key="knowledge-admin",
        label="知识管理",
        index="05",
        capability=Capability.KNOWLEDGE_MANAGE,
    ),
    ModuleDefinition(
        key="organization",
        label="企业组织",
        index="06",
        capability=Capability.ORGANIZATION_MANAGE,
    ),
    ModuleDefinition(
        key="analytics",
        label="运营驾驶舱",
        index="07",
        capability=Capability.ANALYTICS_VIEW,
    ),
    ModuleDefinition(
        key="procurement",
        label="采购申请",
        index="08",
        capability=Capability.PROCUREMENT_REQUEST_SELF_SERVICE,
    ),
    ModuleDefinition(
        key="approval-center",
        label="审批中心",
        index="09",
        capability=Capability.APPROVAL_INBOX_VIEW,
    ),
)
DEFAULT_MODULE_KEYS = frozenset(module.key for module in DEFAULT_MODULES)


class ModuleCatalog:
    def __init__(
        self,
        resolver: CapabilityResolver,
        modules: tuple[ModuleDefinition, ...] = DEFAULT_MODULES,
    ) -> None:
        self._resolver = resolver
        self._modules = modules

    def allowed_modules(
        self,
        db: Session,
        user: User,
    ) -> tuple[ModuleDefinition, ...]:
        return tuple(
            module
            for module in self._modules
            if self._resolver.has(db, user, module.capability)
        )
