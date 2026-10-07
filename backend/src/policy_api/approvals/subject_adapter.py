"""Fail-closed registry for subject-specific, whitelisted read adapters."""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from policy_api.approvals.models import ApprovalInstance
from policy_api.approvals.schemas import SubjectDetail, SubjectSummary
from policy_api.tools.errors import ToolError


class ApprovalError(ToolError):
    """Stable, domain-neutral application error."""


@runtime_checkable
class ApprovalSubjectAdapter(Protocol):
    subject_type: str

    def summary(
        self, db: Session, instance: ApprovalInstance
    ) -> SubjectSummary: ...

    def detail(
        self, db: Session, instance: ApprovalInstance
    ) -> SubjectDetail: ...


class SubjectAdapterRegistry:
    """An immutable adapter snapshot; duplicate subject types are rejected."""

    def __init__(
        self, adapters: Iterable[ApprovalSubjectAdapter] = ()
    ) -> None:
        snapshot: dict[str, ApprovalSubjectAdapter] = {}
        for adapter in tuple(adapters):
            subject_type = getattr(adapter, "subject_type", None)
            if not isinstance(subject_type, str) or not subject_type.strip():
                raise ApprovalError("approval_subject_invalid")
            if subject_type in snapshot:
                raise ApprovalError("approval_subject_duplicate")
            snapshot[subject_type] = adapter
        self._adapters = MappingProxyType(snapshot)

    def require(self, subject_type: str) -> ApprovalSubjectAdapter:
        if not isinstance(subject_type, str):
            raise ApprovalError("approval_subject_not_supported")
        try:
            return self._adapters[subject_type]
        except KeyError:
            raise ApprovalError("approval_subject_not_supported") from None

    def summary(
        self,
        subject_type: str,
        db: Session,
        instance: ApprovalInstance,
    ) -> SubjectSummary:
        rendered = self.require(subject_type).summary(db, instance)
        if type(rendered) is not SubjectSummary:
            raise ApprovalError("approval_subject_invalid")
        return rendered

    def detail(
        self,
        subject_type: str,
        db: Session,
        instance: ApprovalInstance,
    ) -> SubjectDetail:
        rendered = self.require(subject_type).detail(db, instance)
        if type(rendered) is not SubjectDetail:
            raise ApprovalError("approval_subject_invalid")
        return rendered


__all__ = [
    "ApprovalError",
    "ApprovalSubjectAdapter",
    "SubjectAdapterRegistry",
]
