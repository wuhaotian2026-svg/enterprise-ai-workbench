from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from policy_api.hr.enums import LeaveRequestStatus
from policy_api.hr.models import EmployeeProfile, LeaveRequest
from policy_api.models import User
from policy_api.workbench.capabilities import (
    Capability,
    CapabilityResolver,
    OrganizationUnit,
)
from policy_api.workbench.events import ProductEvent
from policy_api.workbench.audit import SecurityAuditEvent
from policy_api.workbench.schemas import AnalyticsResponse, MetricValue


MAX_ANALYTICS_WINDOW = timedelta(days=90)
DEFAULT_ANALYTICS_WINDOW = timedelta(days=7)
MODEL_FAILURE_CODES = frozenset(
    {
        "model_provider_error",
        "model_provider_timeout",
        "model_provider_rate_limited",
        "model_provider_unavailable",
    }
)


class AnalyticsError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AnalyticsWindow:
    start: datetime
    end: datetime


@dataclass(slots=True)
class AnalyticsService:
    capability_resolver: CapabilityResolver = field(
        default_factory=CapabilityResolver
    )

    def overview(
        self,
        db: Session,
        actor: User,
        window: AnalyticsWindow,
        organization_unit_id: UUID | None,
    ) -> AnalyticsResponse:
        organization_ids = self._event_organization_ids(
            db, actor, organization_unit_id
        )
        event_conditions = [
            ProductEvent.occurred_at >= window.start,
            ProductEvent.occurred_at < window.end,
        ]
        if organization_ids is not None:
            event_conditions.append(
                ProductEvent.organization_unit_id.in_(organization_ids)
            )
        aggregate = db.execute(
            select(
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "question_submitted")
                .label("questions_submitted"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "leave_request_submitted")
                .label("leave_requests_submitted"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "tool_planned")
                .label("tools_planned"),
            ).where(*event_conditions)
        ).one()
        pending_conditions = [LeaveRequest.status == LeaveRequestStatus.PENDING]
        if organization_ids is not None:
            pending_conditions.append(
                EmployeeProfile.organization_unit_id.in_(organization_ids)
            )
        pending = db.scalar(
            select(func.count(LeaveRequest.id))
            .join(EmployeeProfile, EmployeeProfile.id == LeaveRequest.employee_id)
            .where(*pending_conditions)
        )
        metrics = {
            "questions_submitted": count_metric(aggregate.questions_submitted),
            "leave_requests_submitted": count_metric(
                aggregate.leave_requests_submitted
            ),
            "tools_planned": count_metric(aggregate.tools_planned),
            "leave_requests_pending": count_metric(pending or 0),
        }
        if organization_unit_id is not None:
            metrics = {
                key: suppress_small_segment(value)
                for key, value in metrics.items()
            }
        return AnalyticsResponse(
            **{
                "from": window.start,
                "to": window.end,
                "organization_unit_id": organization_unit_id,
                "metrics": metrics,
            }
        )

    def knowledge(
        self,
        db: Session,
        actor: User,
        window: AnalyticsWindow,
        organization_unit_id: UUID | None,
    ) -> AnalyticsResponse:
        organization_ids = self._event_organization_ids(
            db, actor, organization_unit_id
        )
        conditions = [
            ProductEvent.occurred_at >= window.start,
            ProductEvent.occurred_at < window.end,
        ]
        if organization_ids is not None:
            conditions.append(
                ProductEvent.organization_unit_id.in_(organization_ids)
            )
        aggregate = db.execute(
            select(
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "question_submitted")
                .label("submitted"),
                func.count(ProductEvent.id)
                .filter(
                    ProductEvent.event_name == "question_answered",
                    ProductEvent.dimensions["has_citations"].as_boolean().is_(True),
                )
                .label("evidence_answered"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "question_abstained")
                .label("abstained"),
                func.count(ProductEvent.id)
                .filter(
                    ProductEvent.event_name
                    == "question_clarification_requested"
                )
                .label("clarified"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "answer_feedback_submitted")
                .label("feedback"),
                func.count(ProductEvent.id)
                .filter(
                    ProductEvent.event_name == "answer_feedback_submitted",
                    ProductEvent.dimensions["helpful"].as_boolean().is_(True),
                )
                .label("helpful"),
                func.count(ProductEvent.id)
                .filter(
                    ProductEvent.event_name == "question_abstained",
                    ProductEvent.dimensions["error_code"]
                    .as_string()
                    .in_(MODEL_FAILURE_CODES),
                )
                .label("model_failures"),
            ).where(*conditions)
        ).one()
        terminal_condition = ProductEvent.event_name.in_(
            (
                "question_answered",
                "question_clarification_requested",
                "question_abstained",
            )
        )
        latency = db.execute(
            select(
                func.count(ProductEvent.duration_ms).label("sample_size"),
                func.percentile_cont(0.5)
                .within_group(ProductEvent.duration_ms)
                .label("p50"),
                func.percentile_cont(0.95)
                .within_group(ProductEvent.duration_ms)
                .label("p95"),
            ).where(
                *conditions,
                terminal_condition,
                ProductEvent.duration_ms.is_not(None),
            )
        ).one()
        metrics = {
            "questions_submitted": count_metric(aggregate.submitted),
            "evidence_answer_rate": ratio_metric(
                aggregate.evidence_answered, aggregate.submitted
            ),
            "clarification_rate": ratio_metric(
                aggregate.clarified, aggregate.submitted
            ),
            "abstention_rate": ratio_metric(
                aggregate.abstained, aggregate.submitted
            ),
            "helpful_feedback_rate": ratio_metric(
                aggregate.helpful, aggregate.feedback
            ),
            "model_failure_rate": ratio_metric(
                aggregate.model_failures, aggregate.submitted
            ),
            "response_latency_p50_ms": percentile_metric(
                latency.p50, sample_size=latency.sample_size
            ),
            "response_latency_p95_ms": percentile_metric(
                latency.p95, sample_size=latency.sample_size
            ),
        }
        if organization_unit_id is not None:
            metrics = {
                key: suppress_small_segment(value)
                for key, value in metrics.items()
            }
        return AnalyticsResponse(
            **{
                "from": window.start,
                "to": window.end,
                "organization_unit_id": organization_unit_id,
                "metrics": metrics,
            }
        )

    def hr_funnel(
        self,
        db: Session,
        actor: User,
        window: AnalyticsWindow,
        organization_unit_id: UUID | None,
    ) -> AnalyticsResponse:
        organization_ids = self._event_organization_ids(
            db, actor, organization_unit_id
        )
        conditions = [
            ProductEvent.occurred_at >= window.start,
            ProductEvent.occurred_at < window.end,
        ]
        if organization_ids is not None:
            conditions.append(
                ProductEvent.organization_unit_id.in_(organization_ids)
            )
        aggregate = db.execute(
            select(
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "hr_turn_submitted")
                .label("turns"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "hr_intent_resolved")
                .label("intents"),
                func.count(ProductEvent.id)
                .filter(
                    ProductEvent.event_name == "tool_planned",
                    ProductEvent.dimensions["risk_level"].as_string() == "write",
                )
                .label("write_plans"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "confirmation_shown")
                .label("shown"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "confirmation_confirmed")
                .label("confirmed"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "leave_request_submitted")
                .label("submitted"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "leave_request_reviewed")
                .label("reviewed"),
            ).where(*conditions)
        ).one()
        metrics = {
            "hr_turns_submitted": count_metric(aggregate.turns),
            "intents_resolved": count_metric(aggregate.intents),
            "write_tools_planned": count_metric(aggregate.write_plans),
            "confirmations_shown": count_metric(aggregate.shown),
            "confirmations_confirmed": count_metric(aggregate.confirmed),
            "leave_requests_submitted": count_metric(aggregate.submitted),
            "leave_requests_reviewed": count_metric(aggregate.reviewed),
            "intent_resolution_rate": ratio_metric(
                aggregate.intents, aggregate.turns
            ),
            "write_tool_plan_rate": ratio_metric(
                aggregate.write_plans, aggregate.intents
            ),
            "confirmation_show_rate": ratio_metric(
                aggregate.shown, aggregate.write_plans
            ),
            "confirmation_confirm_rate": ratio_metric(
                aggregate.confirmed, aggregate.shown
            ),
            "leave_submission_rate": ratio_metric(
                aggregate.submitted, aggregate.confirmed
            ),
            "leave_review_rate": ratio_metric(
                aggregate.reviewed, aggregate.submitted
            ),
        }
        if organization_unit_id is not None:
            metrics = {
                key: suppress_small_segment(value)
                for key, value in metrics.items()
            }
        return AnalyticsResponse(
            **{
                "from": window.start,
                "to": window.end,
                "organization_unit_id": organization_unit_id,
                "metrics": metrics,
            }
        )

    def tools(
        self,
        db: Session,
        actor: User,
        window: AnalyticsWindow,
        organization_unit_id: UUID | None,
    ) -> AnalyticsResponse:
        organization_ids = self._event_organization_ids(
            db, actor, organization_unit_id
        )
        conditions = [
            ProductEvent.occurred_at >= window.start,
            ProductEvent.occurred_at < window.end,
        ]
        if organization_ids is not None:
            conditions.append(
                ProductEvent.organization_unit_id.in_(organization_ids)
            )
        aggregate = db.execute(
            select(
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "tool_planned")
                .label("planned"),
                func.count(ProductEvent.id)
                .filter(
                    ProductEvent.event_name == "tool_planned",
                    ProductEvent.dimensions["risk_level"]
                    .as_string()
                    .in_(("read", "sensitive_read")),
                )
                .label("read_planned"),
                func.count(ProductEvent.id)
                .filter(
                    ProductEvent.event_name == "tool_planned",
                    ProductEvent.dimensions["risk_level"].as_string() == "write",
                )
                .label("write_planned"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "tool_validation_failed")
                .label("validation_failed"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "hr_turn_submitted")
                .label("hr_turns"),
                func.count(ProductEvent.id)
                .filter(
                    ProductEvent.event_name == "hr_flow_error",
                    ProductEvent.dimensions["error_code"]
                    .as_string()
                    .in_(MODEL_FAILURE_CODES),
                )
                .label("provider_failed"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "confirmation_shown")
                .label("confirmations_shown"),
                func.count(ProductEvent.id)
                .filter(ProductEvent.event_name == "confirmation_expired")
                .label("confirmations_expired"),
            ).where(*conditions)
        ).one()
        latency = db.execute(
            select(
                func.count(ProductEvent.duration_ms).label("sample_size"),
                func.percentile_cont(0.5)
                .within_group(ProductEvent.duration_ms)
                .label("p50"),
                func.percentile_cont(0.95)
                .within_group(ProductEvent.duration_ms)
                .label("p95"),
            ).where(
                *conditions,
                ProductEvent.event_name == "tool_read_succeeded",
                ProductEvent.duration_ms.is_not(None),
            )
        ).one()
        metrics = {
            "tools_planned": count_metric(aggregate.planned),
            "read_tools_planned": count_metric(aggregate.read_planned),
            "write_tools_planned": count_metric(aggregate.write_planned),
            "validation_failure_rate": ratio_metric(
                aggregate.validation_failed, aggregate.planned
            ),
            "provider_failure_rate": ratio_metric(
                aggregate.provider_failed, aggregate.hr_turns
            ),
            "confirmation_expiry_rate": ratio_metric(
                aggregate.confirmations_expired,
                aggregate.confirmations_shown,
            ),
            "read_latency_p50_ms": percentile_metric(
                latency.p50, sample_size=latency.sample_size
            ),
            "read_latency_p95_ms": percentile_metric(
                latency.p95, sample_size=latency.sample_size
            ),
        }
        if organization_unit_id is not None:
            metrics = {
                key: suppress_small_segment(value)
                for key, value in metrics.items()
            }
        return AnalyticsResponse(
            **{
                "from": window.start,
                "to": window.end,
                "organization_unit_id": organization_unit_id,
                "metrics": metrics,
            }
        )

    def workflows(
        self,
        db: Session,
        actor: User,
        window: AnalyticsWindow,
        organization_unit_id: UUID | None,
    ) -> AnalyticsResponse:
        organization_ids = self._event_organization_ids(
            db, actor, organization_unit_id
        )
        conditions = []
        if organization_ids is not None:
            conditions.append(
                EmployeeProfile.organization_unit_id.in_(organization_ids)
            )
        reviewed_in_window = and_(
            LeaveRequest.status.in_(
                (LeaveRequestStatus.APPROVED, LeaveRequestStatus.REJECTED)
            ),
            LeaveRequest.reviewed_at >= window.start,
            LeaveRequest.reviewed_at < window.end,
        )
        cancelled_in_window = and_(
            LeaveRequest.status == LeaveRequestStatus.CANCELLED,
            LeaveRequest.cancelled_at >= window.start,
            LeaveRequest.cancelled_at < window.end,
        )
        aggregate = db.execute(
            select(
                func.count(LeaveRequest.id)
                .filter(LeaveRequest.status == LeaveRequestStatus.PENDING)
                .label("pending"),
                func.count(LeaveRequest.id)
                .filter(
                    LeaveRequest.submitted_at >= window.start,
                    LeaveRequest.submitted_at < window.end,
                )
                .label("submitted"),
                func.count(LeaveRequest.id)
                .filter(or_(reviewed_in_window, cancelled_in_window))
                .label("processed"),
                func.count(LeaveRequest.id)
                .filter(LeaveRequest.status == LeaveRequestStatus.APPROVED)
                .label("approved"),
                func.count(LeaveRequest.id)
                .filter(LeaveRequest.status == LeaveRequestStatus.REJECTED)
                .label("rejected"),
                func.count(LeaveRequest.id)
                .filter(LeaveRequest.status == LeaveRequestStatus.CANCELLED)
                .label("cancelled"),
                func.count(LeaveRequest.id)
                .filter(
                    LeaveRequest.status == LeaveRequestStatus.PENDING,
                    LeaveRequest.submitted_at < window.end - timedelta(hours=24),
                )
                .label("pending_over_24h"),
            )
            .join(EmployeeProfile, EmployeeProfile.id == LeaveRequest.employee_id)
            .where(*conditions)
        ).one()
        review_duration_ms = (
            func.extract(
                "epoch", LeaveRequest.reviewed_at - LeaveRequest.submitted_at
            )
            * 1000
        )
        review_latency = db.execute(
            select(
                func.count(LeaveRequest.reviewed_at).label("sample_size"),
                func.percentile_cont(0.5)
                .within_group(review_duration_ms)
                .label("p50"),
                func.percentile_cont(0.95)
                .within_group(review_duration_ms)
                .label("p95"),
            )
            .join(EmployeeProfile, EmployeeProfile.id == LeaveRequest.employee_id)
            .where(*conditions, reviewed_in_window)
        ).one()
        metrics = {
            "leave_requests_pending": count_metric(aggregate.pending),
            "leave_requests_submitted": count_metric(aggregate.submitted),
            "leave_requests_processed": count_metric(aggregate.processed),
            "leave_requests_approved": count_metric(aggregate.approved),
            "leave_requests_rejected": count_metric(aggregate.rejected),
            "leave_requests_cancelled": count_metric(aggregate.cancelled),
            "review_duration_p50_ms": percentile_metric(
                review_latency.p50, sample_size=review_latency.sample_size
            ),
            "review_duration_p95_ms": percentile_metric(
                review_latency.p95, sample_size=review_latency.sample_size
            ),
            "leave_requests_pending_over_24h": count_metric(
                aggregate.pending_over_24h
            ),
        }
        if organization_unit_id is not None:
            metrics = {
                key: suppress_small_segment(value)
                for key, value in metrics.items()
            }
        return AnalyticsResponse(
            **{
                "from": window.start,
                "to": window.end,
                "organization_unit_id": organization_unit_id,
                "metrics": metrics,
            }
        )

    def procurement_funnel(
        self,
        db: Session,
        actor: User,
        window: AnalyticsWindow,
        organization_unit_id: UUID | None,
    ) -> AnalyticsResponse:
        organization_ids = self._event_organization_ids(
            db, actor, organization_unit_id
        )
        conditions = [
            ProductEvent.occurred_at >= window.start,
            ProductEvent.occurred_at < window.end,
        ]
        if organization_ids is not None:
            conditions.append(ProductEvent.organization_unit_id.in_(organization_ids))
        stage = ProductEvent.dimensions["stage"].as_string()
        channel = ProductEvent.dimensions["channel"].as_string()
        aggregate = db.execute(
            select(
                func.count(ProductEvent.id).filter(ProductEvent.event_name == "procurement_request_submitted").label("submitted"),
                func.count(ProductEvent.id).filter(ProductEvent.event_name == "procurement_request_withdrawn").label("withdrawn"),
                func.count(ProductEvent.id).filter(ProductEvent.event_name == "approval_task_approved", stage == "department_review").label("department_approved"),
                func.count(ProductEvent.id).filter(ProductEvent.event_name == "approval_task_rejected", stage == "department_review").label("department_rejected"),
                func.count(ProductEvent.id).filter(ProductEvent.event_name == "approval_task_approved", stage == "procurement_review").label("procurement_approved"),
                func.count(ProductEvent.id).filter(ProductEvent.event_name == "approval_task_rejected", stage == "procurement_review").label("procurement_rejected"),
                func.count(ProductEvent.id).filter(ProductEvent.event_name == "procurement_request_submitted", channel == "ai_confirmation").label("ai_submitted"),
                func.count(ProductEvent.id).filter(ProductEvent.event_name == "confirmation_shown", ProductEvent.dimensions["tool_name"].as_string() == "procurement.submit_request").label("confirmations_shown"),
                func.count(ProductEvent.id).filter(ProductEvent.event_name == "confirmation_confirmed", ProductEvent.dimensions["tool_name"].as_string() == "procurement.submit_request").label("confirmations_confirmed"),
                func.count(ProductEvent.id).filter(ProductEvent.event_name == "tool_validation_failed", ProductEvent.dimensions["tool_name"].as_string().like("procurement.%")).label("validation_failures"),
            ).where(*conditions)
        ).one()

        def latency_for(names: tuple[str, ...], selected_stage: str | None = None):
            latency_conditions = list(conditions)
            latency_conditions.extend((ProductEvent.event_name.in_(names), ProductEvent.duration_ms.is_not(None)))
            if selected_stage is not None:
                latency_conditions.append(stage == selected_stage)
            return db.execute(
                select(
                    func.count(ProductEvent.duration_ms).label("sample_size"),
                    func.percentile_cont(0.5).within_group(ProductEvent.duration_ms).label("p50"),
                    func.percentile_cont(0.95).within_group(ProductEvent.duration_ms).label("p95"),
                ).where(*latency_conditions)
            ).one()

        department_latency = latency_for(("approval_task_approved", "approval_task_rejected"), "department_review")
        procurement_latency = latency_for(("approval_task_approved", "approval_task_rejected"), "procurement_review")
        flow_latency = latency_for(("procurement_request_completed",))

        audit_conditions = [
            SecurityAuditEvent.occurred_at >= window.start,
            SecurityAuditEvent.occurred_at < window.end,
        ]
        if organization_ids is not None:
            audit_conditions.append(
                SecurityAuditEvent.summary["organization_unit_id"].as_string().in_(
                    tuple(str(value) for value in organization_ids)
                )
            )
        audit_counts = db.execute(
            select(
                func.count(SecurityAuditEvent.id).filter(SecurityAuditEvent.event_name == "procurement_permission_denied").label("denials"),
                func.count(SecurityAuditEvent.id).filter(SecurityAuditEvent.event_name == "procurement_operation_conflict").label("conflicts"),
                func.count(SecurityAuditEvent.id).filter(SecurityAuditEvent.event_name == "procurement_operation_replayed").label("replays"),
            ).where(*audit_conditions)
        ).one()
        terminal_audit = or_(
            SecurityAuditEvent.event_name.in_((
                "approval_task_rejected",
                "procurement_request_withdrawn",
            )),
            and_(
                SecurityAuditEvent.event_name == "approval_task_approved",
                SecurityAuditEvent.summary["stage"].as_string()
                == "procurement_review",
            ),
        )
        north_conditions = [
            SecurityAuditEvent.occurred_at >= window.start,
            SecurityAuditEvent.occurred_at < window.end,
            SecurityAuditEvent.target_id.is_not(None),
            or_(
                SecurityAuditEvent.event_name
                == "procurement_request_submitted",
                terminal_audit,
            ),
        ]
        if organization_ids is not None:
            north_conditions.append(
                SecurityAuditEvent.summary["organization_unit_id"]
                .as_string()
                .in_(tuple(str(value) for value in organization_ids))
            )
        audited_requests = (
            select(SecurityAuditEvent.target_id)
            .where(*north_conditions)
            .group_by(SecurityAuditEvent.target_id)
            .having(
                func.count(SecurityAuditEvent.id).filter(
                    SecurityAuditEvent.event_name
                    == "procurement_request_submitted"
                )
                > 0,
                func.count(SecurityAuditEvent.id).filter(terminal_audit) > 0,
            )
            .subquery()
        )
        audited_terminal = db.scalar(
            select(func.count()).select_from(audited_requests)
        ) or 0
        department_total = aggregate.department_approved + aggregate.department_rejected
        procurement_total = aggregate.procurement_approved + aggregate.procurement_rejected
        metrics = {
            "procurement_requests_submitted": count_metric(aggregate.submitted),
            "withdrawal_rate": ratio_metric(aggregate.withdrawn, aggregate.submitted),
            "department_approval_rate": ratio_metric(aggregate.department_approved, department_total),
            "department_rejection_rate": ratio_metric(aggregate.department_rejected, department_total),
            "procurement_approval_rate": ratio_metric(aggregate.procurement_approved, procurement_total),
            "procurement_rejection_rate": ratio_metric(aggregate.procurement_rejected, procurement_total),
            "department_processing_p50_ms": percentile_metric(department_latency.p50, sample_size=department_latency.sample_size),
            "department_processing_p95_ms": percentile_metric(department_latency.p95, sample_size=department_latency.sample_size),
            "procurement_processing_p50_ms": percentile_metric(procurement_latency.p50, sample_size=procurement_latency.sample_size),
            "procurement_processing_p95_ms": percentile_metric(procurement_latency.p95, sample_size=procurement_latency.sample_size),
            "flow_processing_p50_ms": percentile_metric(flow_latency.p50, sample_size=flow_latency.sample_size),
            "flow_processing_p95_ms": percentile_metric(flow_latency.p95, sample_size=flow_latency.sample_size),
            "ai_assisted_submission_rate": ratio_metric(aggregate.ai_submitted, aggregate.submitted),
            "confirmation_conversion_rate": ratio_metric(aggregate.confirmations_confirmed, aggregate.confirmations_shown),
            "tool_validation_failures": count_metric(aggregate.validation_failures),
            "permission_denials": count_metric(audit_counts.denials),
            "state_conflicts": count_metric(audit_counts.conflicts),
            "replays": count_metric(audit_counts.replays),
            "audited_terminal_requests": count_metric(audited_terminal),
        }
        if organization_unit_id is not None:
            metrics = {key: suppress_small_segment(value) for key, value in metrics.items()}
        return AnalyticsResponse(**{
            "from": window.start, "to": window.end,
            "organization_unit_id": organization_unit_id, "metrics": metrics,
        })

    def _event_organization_ids(
        self,
        db: Session,
        actor: User,
        requested_organization_unit_id: UUID | None,
    ) -> frozenset[UUID] | None:
        scope = self.capability_resolver.scope_for(
            db, actor, Capability.ANALYTICS_VIEW
        )
        if scope is None:
            raise AnalyticsError("analytics_scope_forbidden")
        if requested_organization_unit_id is not None:
            if scope.is_global:
                exists = db.scalar(
                    select(OrganizationUnit.id).where(
                        OrganizationUnit.id == requested_organization_unit_id,
                        OrganizationUnit.is_active.is_(True),
                    )
                )
                if exists is None:
                    raise AnalyticsError("analytics_scope_forbidden")
            elif requested_organization_unit_id not in scope.organization_unit_ids:
                raise AnalyticsError("analytics_scope_forbidden")
            return frozenset({requested_organization_unit_id})
        if scope.is_global:
            return None
        return scope.organization_unit_ids


def ratio_metric(numerator: int | float, denominator: int | float) -> MetricValue:
    available = denominator > 0
    return MetricValue(
        numerator=numerator,
        denominator=denominator,
        value=(float(numerator) / float(denominator) if available else None),
        available=available,
        sample_size=max(0, int(denominator)),
    )


def count_metric(count: int) -> MetricValue:
    normalized = max(0, int(count))
    return MetricValue(
        numerator=normalized,
        denominator=None,
        value=float(normalized),
        available=True,
        sample_size=normalized,
    )


def percentile_metric(
    value: int | float | None, *, sample_size: int
) -> MetricValue:
    available = value is not None and sample_size > 0
    normalized = float(value) if value is not None else None
    return MetricValue(
        numerator=normalized,
        denominator=None,
        value=normalized if available else None,
        available=available,
        sample_size=max(0, int(sample_size)),
    )


def suppress_small_segment(
    metric: MetricValue, *, minimum_sample_size: int = 5
) -> MetricValue:
    if metric.sample_size >= minimum_sample_size:
        return metric
    return metric.model_copy(
        update={
            "numerator": None,
            "denominator": None,
            "value": None,
            "available": False,
        }
    )


def resolve_window(
    start: datetime | None,
    end: datetime | None,
    *,
    now: datetime | None = None,
) -> AnalyticsWindow:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise AnalyticsError("analytics_range_invalid")
    resolved_end = end or current
    resolved_start = start or resolved_end - DEFAULT_ANALYTICS_WINDOW
    if any(
        value.tzinfo is None or value.utcoffset() is None
        for value in (resolved_start, resolved_end)
    ):
        raise AnalyticsError("analytics_range_invalid")
    normalized_now = current.astimezone(timezone.utc)
    normalized_start = resolved_start.astimezone(timezone.utc)
    normalized_end = resolved_end.astimezone(timezone.utc)
    duration = normalized_end - normalized_start
    if (
        duration <= timedelta(0)
        or duration > MAX_ANALYTICS_WINDOW
        or normalized_start > normalized_now
        or normalized_end > normalized_now
    ):
        raise AnalyticsError("analytics_range_invalid")
    return AnalyticsWindow(start=normalized_start, end=normalized_end)
