from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from policy_api.database import create_database_engine
from policy_api.hr.models import EmployeeProfile, HrTurn, LeaveRequest
from policy_api.hr.repository import find_review_request
from policy_api.models import User
from policy_api.workbench.analytics import AnalyticsService, resolve_window
from policy_api.workbench.audit import SecurityAuditEvent
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityResolver,
    CapabilityScope,
)
from policy_api.workbench.catalog import DEFAULT_MODULES, ModuleCatalog
from policy_api.workbench.events import ProductEvent


class FoundationEvaluationError(ValueError):
    pass


class FoundationObservations(NamedTuple):
    module_authorization_passes: int
    module_authorization_cases: int
    scoped_hr_forbidden_successes: int
    scoped_hr_forbidden_cases: int
    product_event_sensitive_hits: int
    product_event_rows: int
    security_audit_sensitive_hits: int
    security_audit_rows: int
    duplicate_event_resources: int
    duplicate_event_resource_cases: int
    zero_denominator_false_perfect_scores: int
    zero_denominator_cases: int
    metric_contract_passes: int
    metric_contract_cases: int


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _validated_observations(
    observations: FoundationObservations,
) -> FoundationObservations:
    if any(value < 0 for value in observations):
        raise FoundationEvaluationError("observation_invalid")
    pairs = (
        (
            observations.module_authorization_passes,
            observations.module_authorization_cases,
        ),
        (
            observations.scoped_hr_forbidden_successes,
            observations.scoped_hr_forbidden_cases,
        ),
        (
            observations.product_event_sensitive_hits,
            observations.product_event_rows,
        ),
        (
            observations.security_audit_sensitive_hits,
            observations.security_audit_rows,
        ),
        (
            observations.duplicate_event_resources,
            observations.duplicate_event_resource_cases,
        ),
        (
            observations.zero_denominator_false_perfect_scores,
            observations.zero_denominator_cases,
        ),
        (
            observations.metric_contract_passes,
            observations.metric_contract_cases,
        ),
    )
    if any(numerator > denominator for numerator, denominator in pairs):
        raise FoundationEvaluationError("observation_invalid")
    return observations


def build_report(observations: FoundationObservations) -> dict[str, Any]:
    values = _validated_observations(observations)
    passed = (
        values.module_authorization_passes
        == values.module_authorization_cases
        and values.scoped_hr_forbidden_successes == 0
        and values.product_event_sensitive_hits == 0
        and values.security_audit_sensitive_hits == 0
        and values.duplicate_event_resources == 0
        and values.zero_denominator_false_perfect_scores == 0
        and values.metric_contract_passes == values.metric_contract_cases
    )
    return {
        "schema_version": "workbench-foundation.v1",
        "status": "passed" if passed else "failed",
        "module_authorization_cases": {
            "passed": values.module_authorization_passes,
            "total": values.module_authorization_cases,
            "rate": _rate(
                values.module_authorization_passes,
                values.module_authorization_cases,
            ),
        },
        "scoped_hr_forbidden_successes": {
            "count": values.scoped_hr_forbidden_successes,
            "total": values.scoped_hr_forbidden_cases,
            "rate": _rate(
                values.scoped_hr_forbidden_successes,
                values.scoped_hr_forbidden_cases,
            ),
        },
        "product_event_sensitive_hits": {
            "count": values.product_event_sensitive_hits,
            "total": values.product_event_rows,
            "rate": _rate(
                values.product_event_sensitive_hits,
                values.product_event_rows,
            ),
        },
        "security_audit_sensitive_hits": {
            "count": values.security_audit_sensitive_hits,
            "total": values.security_audit_rows,
            "rate": _rate(
                values.security_audit_sensitive_hits,
                values.security_audit_rows,
            ),
        },
        "duplicate_event_resources": {
            "count": values.duplicate_event_resources,
            "total": values.duplicate_event_resource_cases,
            "rate": _rate(
                values.duplicate_event_resources,
                values.duplicate_event_resource_cases,
            ),
        },
        "zero_denominator_false_perfect_scores": {
            "count": values.zero_denominator_false_perfect_scores,
            "total": values.zero_denominator_cases,
            "rate": _rate(
                values.zero_denominator_false_perfect_scores,
                values.zero_denominator_cases,
            ),
        },
        "metric_contract_cases": {
            "passed": values.metric_contract_passes,
            "total": values.metric_contract_cases,
            "rate": _rate(
                values.metric_contract_passes,
                values.metric_contract_cases,
            ),
        },
    }


def scan_sensitive_rows(
    rows: Iterable[tuple[object, object]],
    *,
    sensitive_values: Sequence[str],
) -> list[str]:
    needles = tuple(
        value.casefold()
        for value in sensitive_values
        if isinstance(value, str) and value.strip()
    )
    hits: list[str] = []
    for row_id, payload in rows:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).casefold()
        if any(needle in serialized for needle in needles):
            hits.append(str(row_id))
    return hits


def validate_database_target(database_url: str) -> None:
    try:
        database_name = make_url(database_url).database or ""
    except Exception as exc:
        raise FoundationEvaluationError("test_database_required") from exc
    if not database_name.casefold().endswith("_test"):
        raise FoundationEvaluationError("test_database_required")


def write_report_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _sensitive_source_values(db: Session) -> tuple[str, ...]:
    values = {"password", "token", "secret", "cookie"}
    values.update(
        content
        for content in db.scalars(select(HrTurn.content))
        if isinstance(content, str) and content.strip()
    )
    for reason, rejection_reason in db.execute(
        select(LeaveRequest.reason, LeaveRequest.rejection_reason)
    ):
        if reason and reason.strip():
            values.add(reason)
        if rejection_reason and rejection_reason.strip():
            values.add(rejection_reason)
    return tuple(sorted(values))


def _module_authorization_observations(db: Session) -> tuple[int, int]:
    resolver = CapabilityResolver()
    catalog = ModuleCatalog(resolver)
    passes = 0
    cases = 0
    for user in db.scalars(select(User).where(User.is_active.is_(True))):
        allowed_keys = {module.key for module in catalog.allowed_modules(db, user)}
        for module in DEFAULT_MODULES:
            cases += 1
            expected = resolver.has(db, user, module.capability)
            if (module.key in allowed_keys) is expected:
                passes += 1
    return passes, cases


def _scoped_hr_observations(db: Session) -> tuple[int, int]:
    resolver = CapabilityResolver()
    request_rows = list(
        db.execute(
            select(LeaveRequest.id, EmployeeProfile.organization_unit_id).join(
                EmployeeProfile,
                EmployeeProfile.id == LeaveRequest.employee_id,
            )
        )
    )
    forbidden_successes = 0
    cases = 0
    for user in db.scalars(select(User).where(User.is_active.is_(True))):
        scope = resolver.scope_for(db, user, Capability.HR_LEAVE_REVIEW)
        if scope is not None and scope.is_global:
            continue
        if scope is None:
            scope = CapabilityScope(
                is_global=False,
                organization_unit_ids=frozenset(),
            )
        for request_id, organization_unit_id in request_rows:
            if organization_unit_id in scope.organization_unit_ids:
                continue
            cases += 1
            if find_review_request(
                db,
                request_id=request_id,
                scope=scope,
            ) is not None:
                forbidden_successes += 1
    return forbidden_successes, cases


def _event_privacy_observations(
    db: Session,
    sensitive_values: Sequence[str],
) -> tuple[int, int, int, int, int, int]:
    product_events = list(db.scalars(select(ProductEvent)))
    security_events = list(db.scalars(select(SecurityAuditEvent)))
    product_hits = scan_sensitive_rows(
        (
            (
                event.event_id,
                {
                    "event_name": event.event_name,
                    "module_key": event.module_key,
                    "request_id": event.request_id,
                    "outcome": event.outcome,
                    "dimensions": event.dimensions,
                },
            )
            for event in product_events
        ),
        sensitive_values=sensitive_values,
    )
    security_hits = scan_sensitive_rows(
        (
            (
                event.id,
                {
                    "event_name": event.event_name,
                    "target_type": event.target_type,
                    "request_id": event.request_id,
                    "outcome": event.outcome,
                    "summary": event.summary,
                },
            )
            for event in security_events
        ),
        sensitive_values=sensitive_values,
    )
    duplicate_resources = db.scalar(
        select(func.count())
        .select_from(
            select(ProductEvent.event_id)
            .group_by(ProductEvent.event_id)
            .having(func.count(ProductEvent.id) > 1)
            .subquery()
        )
    ) or 0
    return (
        len(product_hits),
        len(product_events),
        len(security_hits),
        len(security_events),
        duplicate_resources,
        len(product_events),
    )


def _metric_observations(db: Session) -> tuple[int, int, int, int]:
    service = AnalyticsService()
    now = datetime.now(timezone.utc)
    window = resolve_window(now - timedelta(days=7), now, now=now)
    zero_denominator_false_perfect = 0
    zero_denominator_cases = 0
    contract_passes = 0
    contract_cases = 0
    for user in db.scalars(select(User).where(User.is_active.is_(True))):
        if not CapabilityResolver().has(db, user, Capability.ANALYTICS_VIEW):
            continue
        for method in (
            service.overview,
            service.knowledge,
            service.hr_funnel,
            service.tools,
            service.workflows,
        ):
            response = method(db, user, window, None)
            for metric in response.metrics.values():
                contract_cases += 1
                valid = (
                    metric.metric_version == "v1"
                    and metric.sample_size >= 0
                    and (metric.available or metric.value is None)
                    and (
                        metric.denominator != 0
                        or (metric.value is None and not metric.available)
                    )
                )
                if valid:
                    contract_passes += 1
                if metric.denominator == 0:
                    zero_denominator_cases += 1
                    if metric.value == 1.0:
                        zero_denominator_false_perfect += 1
    return (
        zero_denominator_false_perfect,
        zero_denominator_cases,
        contract_passes,
        contract_cases,
    )


def evaluate_database(db: Session) -> dict[str, Any]:
    module_passes, module_cases = _module_authorization_observations(db)
    forbidden_successes, forbidden_cases = _scoped_hr_observations(db)
    privacy = _event_privacy_observations(db, _sensitive_source_values(db))
    metric_values = _metric_observations(db)
    observations = FoundationObservations(
        module_authorization_passes=module_passes,
        module_authorization_cases=module_cases,
        scoped_hr_forbidden_successes=forbidden_successes,
        scoped_hr_forbidden_cases=forbidden_cases,
        product_event_sensitive_hits=privacy[0],
        product_event_rows=privacy[1],
        security_audit_sensitive_hits=privacy[2],
        security_audit_rows=privacy[3],
        duplicate_event_resources=privacy[4],
        duplicate_event_resource_cases=privacy[5],
        zero_denominator_false_perfect_scores=metric_values[0],
        zero_denominator_cases=metric_values[1],
        metric_contract_passes=metric_values[2],
        metric_contract_cases=metric_values[3],
    )
    report = build_report(observations)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    return report


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate workbench foundation quality on a test database."
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    validate_database_target(args.database_url)
    engine = create_database_engine(args.database_url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                with Session(bind=connection) as db:
                    report = evaluate_database(db)
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
    write_report_atomic(args.output, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
