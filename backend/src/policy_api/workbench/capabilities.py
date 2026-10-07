from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from policy_api.hr.models import EmployeeProfile
from policy_api.models import (
    Base,
    TimestampMixin,
    User,
    UserRole,
    UUIDPrimaryKeyMixin,
)
from policy_api.workbench.audit import (
    SecurityAuditEvent,
    SecurityAuditValidationError,
    append_security_audit,
)


class Capability(str, Enum):
    KNOWLEDGE_ASK = "knowledge.ask"
    KNOWLEDGE_MANAGE = "knowledge.manage"
    HR_LEAVE_SELF_SERVICE = "hr.leave.self_service"
    HR_LEAVE_REVIEW = "hr.leave.review"
    PROCUREMENT_REQUEST_SELF_SERVICE = "procurement.request.self_service"
    APPROVAL_INBOX_VIEW = "approval.inbox.view"
    PROCUREMENT_DEPARTMENT_REVIEW = "procurement.department.review"
    PROCUREMENT_FINAL_REVIEW = "procurement.final.review"
    ORGANIZATION_MANAGE = "organization.manage"
    ANALYTICS_VIEW = "analytics.view"


class ScopeKind(str, Enum):
    GLOBAL = "global"
    UNIT_SUBTREE = "unit_subtree"


@dataclass(frozen=True, slots=True)
class CapabilityScope:
    is_global: bool
    organization_unit_ids: frozenset[uuid.UUID]


class OrganizationUnit(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organization_units"
    __table_args__ = (
        CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name="ck_organization_unit_parent_not_self",
        ),
    )

    code: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organization_units.id")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class CapabilityGrant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "capability_grants"
    __table_args__ = (
        CheckConstraint(
            "(scope_kind = 'global' AND organization_unit_id IS NULL) OR "
            "(scope_kind = 'unit_subtree' AND organization_unit_id IS NOT NULL)",
            name="ck_capability_grant_scope_shape",
        ),
        Index(
            "uq_capability_grants_active_global",
            "user_id",
            "capability",
            unique=True,
            postgresql_where=text("scope_kind = 'global' AND is_active = true"),
        ),
        Index(
            "uq_capability_grants_active_unit_subtree",
            "user_id",
            "capability",
            "organization_unit_id",
            unique=True,
            postgresql_where=text(
                "scope_kind = 'unit_subtree' AND is_active = true"
            ),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    capability: Mapped[str] = mapped_column(String(80), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    organization_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organization_units.id")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class CapabilityResolver:
    def has(self, db: Session, user: User, capability: Capability) -> bool:
        return self.scope_for(db, user, capability) is not None

    def scope_for(
        self,
        db: Session,
        user: User,
        capability: Capability,
    ) -> CapabilityScope | None:
        if not user.is_active:
            return None
        if capability == Capability.KNOWLEDGE_ASK:
            return CapabilityScope(
                is_global=True,
                organization_unit_ids=frozenset(),
            )
        if capability == Capability.HR_LEAVE_SELF_SERVICE:
            employee_is_active = db.scalar(
                select(EmployeeProfile.is_active).where(
                    EmployeeProfile.user_id == user.id
                )
            )
            if (
                user.role in {UserRole.EMPLOYEE, UserRole.HR}
                and employee_is_active
            ):
                return CapabilityScope(
                    is_global=True,
                    organization_unit_ids=frozenset(),
                )
            return None
        if capability == Capability.PROCUREMENT_REQUEST_SELF_SERVICE:
            employee_is_active = db.scalar(
                select(EmployeeProfile.is_active).where(
                    EmployeeProfile.user_id == user.id
                )
            )
            if not employee_is_active:
                return None
            if user.role in {UserRole.EMPLOYEE, UserRole.HR}:
                return CapabilityScope(
                    is_global=True,
                    organization_unit_ids=frozenset(),
                )

        grants = list(
            db.scalars(
                select(CapabilityGrant).where(
                    CapabilityGrant.user_id == user.id,
                    CapabilityGrant.capability == capability.value,
                    CapabilityGrant.is_active.is_(True),
                )
            )
        )
        if any(
            grant.scope_kind == ScopeKind.GLOBAL.value for grant in grants
        ):
            return CapabilityScope(
                is_global=True,
                organization_unit_ids=frozenset(),
            )

        roots = {
            grant.organization_unit_id
            for grant in grants
            if grant.scope_kind == ScopeKind.UNIT_SUBTREE.value
            and grant.organization_unit_id is not None
        }
        if not roots:
            return None

        descendants = (
            select(OrganizationUnit.id)
            .where(
                OrganizationUnit.id.in_(roots),
                OrganizationUnit.is_active.is_(True),
            )
            .cte(name="authorized_units", recursive=True)
        )
        descendants = descendants.union_all(
            select(OrganizationUnit.id)
            .join(descendants, OrganizationUnit.parent_id == descendants.c.id)
            .where(OrganizationUnit.is_active.is_(True))
        )
        unit_ids = frozenset(db.scalars(select(descendants.c.id)))
        return (
            CapabilityScope(
                is_global=False,
                organization_unit_ids=unit_ids,
            )
            if unit_ids
            else None
        )


class OrganizationManagementError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OrganizationService:
    def list_units(self, db: Session) -> list[OrganizationUnit]:
        return list(db.scalars(select(OrganizationUnit).order_by(OrganizationUnit.code)))

    def create_unit(
        self,
        db: Session,
        *,
        actor: User,
        operation_id: uuid.UUID,
        code: str,
        name: str,
        parent_id: uuid.UUID | None,
        request_id: str | None,
    ) -> OrganizationUnit:
        try:
            replay = self._replay_unit_create(
                db,
                actor_id=actor.id,
                operation_id=operation_id,
                code=code,
                name=name,
                parent_id=parent_id,
            )
            if replay is not None:
                db.commit()
                db.refresh(replay)
                return replay
            if parent_id is not None:
                self._active_unit(db, parent_id)
            unit = OrganizationUnit(
                code=code,
                name=name,
                parent_id=parent_id,
                is_active=True,
            )
            db.add(unit)
            db.flush()
            append_security_audit(
                db,
                event_name="organization_unit_created",
                actor_user_id=actor.id,
                target_type="organization_unit",
                target_id=unit.id,
                operation_id=operation_id,
                outcome="succeeded",
                request_id=request_id,
                summary={
                    "organization_unit_id": unit.id,
                    "parent_id": parent_id,
                    "changed_fields": ["code", "name", "parent_id"],
                },
            )
            db.commit()
            db.refresh(unit)
            return unit
        except IntegrityError:
            db.rollback()
            raise OrganizationManagementError(
                "organization_unit_conflict"
            ) from None
        except Exception as exc:
            db.rollback()
            self._reraise(exc)

    def update_unit(
        self,
        db: Session,
        *,
        actor: User,
        unit_id: uuid.UUID,
        operation_id: uuid.UUID,
        changes: Mapping[str, object],
        request_id: str | None,
    ) -> OrganizationUnit:
        try:
            replay = self._operation(db, actor.id, operation_id)
            if replay is not None:
                if replay.target_id != unit_id or replay.event_name not in {
                    "organization_unit_updated",
                    "organization_unit_deactivated",
                }:
                    raise OrganizationManagementError(
                        "security_audit_operation_conflict"
                    )
                unit = self._unit(db, unit_id)
                if (
                    replay.summary.get("changed_fields") != sorted(changes)
                    or any(getattr(unit, field) != value for field, value in changes.items())
                ):
                    raise OrganizationManagementError(
                        "security_audit_operation_conflict"
                    )
                db.commit()
                db.refresh(unit)
                return unit

            unit = self._unit(db, unit_id, lock=True)
            changed_fields = sorted(changes)
            if "parent_id" in changes:
                parent_id = changes["parent_id"]
                if parent_id is not None and not isinstance(parent_id, uuid.UUID):
                    raise OrganizationManagementError(
                        "organization_unit_not_found"
                    )
                if parent_id is not None:
                    self._active_unit(db, parent_id)
                    if self._would_create_cycle(db, unit.id, parent_id):
                        raise OrganizationManagementError(
                            "organization_unit_cycle"
                        )
                unit.parent_id = parent_id
            if changes.get("is_active") is False and unit.is_active:
                if self._subtree_has_active_employee(db, unit.id):
                    raise OrganizationManagementError(
                        "organization_unit_not_empty"
                    )
            for field in ("code", "name", "is_active"):
                if field in changes:
                    setattr(unit, field, changes[field])
            db.flush()

            deactivation_only = (
                changed_fields == ["is_active"] and unit.is_active is False
            )
            event_name = (
                "organization_unit_deactivated"
                if deactivation_only
                else "organization_unit_updated"
            )
            summary: dict[str, object] = {
                "organization_unit_id": unit.id,
                "changed_fields": changed_fields,
            }
            if event_name == "organization_unit_updated":
                summary["parent_id"] = unit.parent_id
            append_security_audit(
                db,
                event_name=event_name,
                actor_user_id=actor.id,
                target_type="organization_unit",
                target_id=unit.id,
                operation_id=operation_id,
                outcome="succeeded",
                request_id=request_id,
                summary=summary,
            )
            db.commit()
            db.refresh(unit)
            return unit
        except IntegrityError:
            db.rollback()
            raise OrganizationManagementError(
                "organization_unit_conflict"
            ) from None
        except Exception as exc:
            db.rollback()
            self._reraise(exc)

    def list_employees(self, db: Session) -> list[EmployeeProfile]:
        return list(
            db.scalars(
                select(EmployeeProfile).order_by(EmployeeProfile.employee_number)
            )
        )

    def update_employee_assignment(
        self,
        db: Session,
        *,
        actor: User,
        employee_id: uuid.UUID,
        operation_id: uuid.UUID,
        organization_unit_id: uuid.UUID | None,
        manager_employee_id: uuid.UUID | None,
        request_id: str | None,
    ) -> EmployeeProfile:
        try:
            replay = self._operation(db, actor.id, operation_id)
            if replay is not None:
                if (
                    replay.event_name != "employee_assignment_updated"
                    or replay.target_id != employee_id
                ):
                    raise OrganizationManagementError(
                        "security_audit_operation_conflict"
                    )
                employee = self._employee(db, employee_id)
                if (
                    employee.organization_unit_id != organization_unit_id
                    or employee.manager_employee_id != manager_employee_id
                ):
                    raise OrganizationManagementError(
                        "security_audit_operation_conflict"
                    )
                db.commit()
                db.refresh(employee)
                return employee

            employee = self._employee(db, employee_id, lock=True)
            if organization_unit_id is not None:
                self._active_unit(db, organization_unit_id)
            if manager_employee_id is not None:
                if manager_employee_id == employee.id:
                    raise OrganizationManagementError(
                        "manager_assignment_invalid"
                    )
                manager = self._employee(db, manager_employee_id)
                if not manager.is_active:
                    raise OrganizationManagementError(
                        "manager_assignment_invalid"
                    )
                if (
                    organization_unit_id is None
                    or manager.organization_unit_id is None
                    or not self._unit_in_active_subtree(
                        db,
                        root_id=manager.organization_unit_id,
                        candidate_id=organization_unit_id,
                    )
                ):
                    raise OrganizationManagementError(
                        "manager_assignment_invalid"
                    )
            employee.organization_unit_id = organization_unit_id
            employee.manager_employee_id = manager_employee_id
            db.flush()
            append_security_audit(
                db,
                event_name="employee_assignment_updated",
                actor_user_id=actor.id,
                target_type="employee_assignment",
                target_id=employee.id,
                operation_id=operation_id,
                outcome="succeeded",
                request_id=request_id,
                summary={
                    "employee_id": employee.id,
                    "organization_unit_id": organization_unit_id,
                    "manager_employee_id": manager_employee_id,
                    "changed_fields": [
                        "manager_employee_id",
                        "organization_unit_id",
                    ],
                },
            )
            db.commit()
            db.refresh(employee)
            return employee
        except Exception as exc:
            db.rollback()
            self._reraise(exc)

    def list_grants(self, db: Session) -> list[CapabilityGrant]:
        return list(
            db.scalars(
                select(CapabilityGrant).order_by(
                    CapabilityGrant.created_at,
                    CapabilityGrant.id,
                )
            )
        )

    def create_grant(
        self,
        db: Session,
        *,
        actor: User,
        operation_id: uuid.UUID,
        user_id: uuid.UUID,
        capability: Capability,
        scope_kind: ScopeKind,
        organization_unit_id: uuid.UUID | None,
        request_id: str | None,
    ) -> CapabilityGrant:
        try:
            replay = self._operation(db, actor.id, operation_id)
            if replay is not None:
                if replay.event_name not in {
                    "capability_grant_created",
                    "capability_grant_reactivated",
                } or replay.target_id is None:
                    raise OrganizationManagementError(
                        "security_audit_operation_conflict"
                    )
                grant = self._grant(db, replay.target_id)
                if (
                    grant.user_id != user_id
                    or grant.capability != capability.value
                    or grant.scope_kind != scope_kind.value
                    or grant.organization_unit_id != organization_unit_id
                    or not grant.is_active
                ):
                    raise OrganizationManagementError(
                        "security_audit_operation_conflict"
                    )
                db.commit()
                db.refresh(grant)
                return grant

            user = db.get(User, user_id)
            if user is None or not user.is_active:
                raise OrganizationManagementError(
                    "capability_grant_not_found"
                )
            self._validate_grant_scope(db, scope_kind, organization_unit_id)
            semantic_filter = (
                CapabilityGrant.user_id == user_id,
                CapabilityGrant.capability == capability.value,
                CapabilityGrant.scope_kind == scope_kind.value,
                CapabilityGrant.organization_unit_id.is_(None)
                if organization_unit_id is None
                else CapabilityGrant.organization_unit_id == organization_unit_id,
            )
            existing = db.scalar(
                select(CapabilityGrant)
                .where(*semantic_filter)
                .order_by(CapabilityGrant.created_at)
                .with_for_update()
            )
            if existing is not None and existing.is_active:
                raise OrganizationManagementError(
                    "capability_grant_conflict"
                )
            if existing is None:
                grant = CapabilityGrant(
                    user_id=user_id,
                    capability=capability.value,
                    scope_kind=scope_kind.value,
                    organization_unit_id=organization_unit_id,
                    is_active=True,
                )
                db.add(grant)
                event_name = "capability_grant_created"
            else:
                grant = existing
                grant.is_active = True
                event_name = "capability_grant_reactivated"
            db.flush()
            append_security_audit(
                db,
                event_name=event_name,
                actor_user_id=actor.id,
                target_type="capability_grant",
                target_id=grant.id,
                operation_id=operation_id,
                outcome="succeeded",
                request_id=request_id,
                summary=self._grant_summary(grant),
            )
            db.commit()
            db.refresh(grant)
            return grant
        except IntegrityError:
            db.rollback()
            raise OrganizationManagementError(
                "capability_grant_conflict"
            ) from None
        except Exception as exc:
            db.rollback()
            self._reraise(exc)

    def revoke_grant(
        self,
        db: Session,
        *,
        actor: User,
        grant_id: uuid.UUID,
        operation_id: uuid.UUID,
        request_id: str | None,
    ) -> CapabilityGrant:
        try:
            replay = self._operation(db, actor.id, operation_id)
            if replay is not None:
                if (
                    replay.event_name != "capability_grant_revoked"
                    or replay.target_id != grant_id
                ):
                    raise OrganizationManagementError(
                        "security_audit_operation_conflict"
                    )
                grant = self._grant(db, grant_id)
                if grant.is_active:
                    raise OrganizationManagementError(
                        "security_audit_operation_conflict"
                    )
                db.commit()
                db.refresh(grant)
                return grant
            grant = self._grant(db, grant_id, lock=True)
            grant.is_active = False
            db.flush()
            append_security_audit(
                db,
                event_name="capability_grant_revoked",
                actor_user_id=actor.id,
                target_type="capability_grant",
                target_id=grant.id,
                operation_id=operation_id,
                outcome="succeeded",
                request_id=request_id,
                summary=self._grant_summary(grant),
            )
            db.commit()
            db.refresh(grant)
            return grant
        except Exception as exc:
            db.rollback()
            self._reraise(exc)

    @staticmethod
    def _operation(
        db: Session,
        actor_id: uuid.UUID,
        operation_id: uuid.UUID,
    ) -> SecurityAuditEvent | None:
        return db.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.actor_user_id == actor_id,
                SecurityAuditEvent.operation_id == operation_id,
            )
        )

    def _replay_unit_create(
        self,
        db: Session,
        *,
        actor_id: uuid.UUID,
        operation_id: uuid.UUID,
        code: str,
        name: str,
        parent_id: uuid.UUID | None,
    ) -> OrganizationUnit | None:
        operation = self._operation(db, actor_id, operation_id)
        if operation is None:
            return None
        if (
            operation.event_name != "organization_unit_created"
            or operation.target_id is None
        ):
            raise OrganizationManagementError(
                "security_audit_operation_conflict"
            )
        unit = self._unit(db, operation.target_id)
        if (unit.code, unit.name, unit.parent_id) != (code, name, parent_id):
            raise OrganizationManagementError(
                "security_audit_operation_conflict"
            )
        return unit

    @staticmethod
    def _unit(
        db: Session,
        unit_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> OrganizationUnit:
        query = select(OrganizationUnit).where(OrganizationUnit.id == unit_id)
        unit = db.scalar(query.with_for_update() if lock else query)
        if unit is None:
            raise OrganizationManagementError("organization_unit_not_found")
        return unit

    def _active_unit(self, db: Session, unit_id: uuid.UUID) -> OrganizationUnit:
        unit = self._unit(db, unit_id)
        if not unit.is_active:
            raise OrganizationManagementError("organization_unit_not_found")
        return unit

    @staticmethod
    def _employee(
        db: Session,
        employee_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> EmployeeProfile:
        query = select(EmployeeProfile).where(EmployeeProfile.id == employee_id)
        employee = db.scalar(query.with_for_update() if lock else query)
        if employee is None:
            raise OrganizationManagementError("employee_assignment_invalid")
        return employee

    @staticmethod
    def _grant(
        db: Session,
        grant_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> CapabilityGrant:
        query = select(CapabilityGrant).where(CapabilityGrant.id == grant_id)
        grant = db.scalar(query.with_for_update() if lock else query)
        if grant is None:
            raise OrganizationManagementError("capability_grant_not_found")
        return grant

    @staticmethod
    def _descendants(unit_id: uuid.UUID):
        descendants = (
            select(OrganizationUnit.id)
            .where(OrganizationUnit.id == unit_id)
            .cte(name="organization_descendants", recursive=True)
        )
        return descendants.union_all(
            select(OrganizationUnit.id).join(
                descendants,
                OrganizationUnit.parent_id == descendants.c.id,
            )
        )

    def _would_create_cycle(
        self,
        db: Session,
        unit_id: uuid.UUID,
        parent_id: uuid.UUID,
    ) -> bool:
        descendants = self._descendants(unit_id)
        return db.scalar(
            select(descendants.c.id).where(descendants.c.id == parent_id)
        ) is not None

    def _subtree_has_active_employee(
        self,
        db: Session,
        unit_id: uuid.UUID,
    ) -> bool:
        descendants = self._descendants(unit_id)
        return db.scalar(
            select(EmployeeProfile.id)
            .where(
                EmployeeProfile.organization_unit_id.in_(
                    select(descendants.c.id)
                ),
                EmployeeProfile.is_active.is_(True),
            )
            .limit(1)
        ) is not None

    def _unit_in_active_subtree(
        self,
        db: Session,
        *,
        root_id: uuid.UUID,
        candidate_id: uuid.UUID,
    ) -> bool:
        descendants = (
            select(OrganizationUnit.id)
            .where(
                OrganizationUnit.id == root_id,
                OrganizationUnit.is_active.is_(True),
            )
            .cte(name="active_manager_subtree", recursive=True)
        )
        descendants = descendants.union_all(
            select(OrganizationUnit.id)
            .join(descendants, OrganizationUnit.parent_id == descendants.c.id)
            .where(OrganizationUnit.is_active.is_(True))
        )
        return db.scalar(
            select(descendants.c.id).where(descendants.c.id == candidate_id)
        ) is not None

    def _validate_grant_scope(
        self,
        db: Session,
        scope_kind: ScopeKind,
        organization_unit_id: uuid.UUID | None,
    ) -> None:
        if scope_kind == ScopeKind.GLOBAL:
            if organization_unit_id is not None:
                raise OrganizationManagementError(
                    "capability_grant_conflict"
                )
            return
        if organization_unit_id is None:
            raise OrganizationManagementError("capability_grant_conflict")
        self._active_unit(db, organization_unit_id)

    @staticmethod
    def _grant_summary(grant: CapabilityGrant) -> dict[str, object]:
        return {
            "grant_id": grant.id,
            "capability": grant.capability,
            "scope_kind": grant.scope_kind,
            "organization_unit_id": grant.organization_unit_id,
        }

    @staticmethod
    def _reraise(exc: Exception) -> None:
        if isinstance(exc, OrganizationManagementError):
            raise exc
        if isinstance(exc, SecurityAuditValidationError):
            raise OrganizationManagementError(exc.code) from None
        raise exc
