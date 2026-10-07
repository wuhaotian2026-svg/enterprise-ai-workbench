import {
  CheckCircle2,
  CircleDashed,
  Clock3,
  FileText,
  PackageOpen,
  XCircle,
} from "lucide-react";
import type { ComponentType } from "react";
import type {
  ApprovalSubjectDetail,
  ApprovalSubjectType,
  ApprovalTimelineEntry,
  ProcurementApprovalSubjectDetail,
} from "./types";

export type SubjectRendererProps = {
  subject: ApprovalSubjectDetail;
};

export type SubjectRenderer = ComponentType<SubjectRendererProps>;

const CATEGORY_LABELS: Record<ProcurementApprovalSubjectDetail["items"][number]["category"], string> = {
  office_supplies: "办公用品",
  it_equipment: "IT 设备",
  software_service: "软件服务",
  professional_service: "专业服务",
  other: "其他",
};

const TIMELINE_STATUS_LABELS: Record<ApprovalTimelineEntry["status"], string> = {
  submitted: "已提交",
  waiting: "等待中",
  pending: "待处理",
  approved: "已批准",
  rejected: "已拒绝",
  cancelled: "已撤回",
};

function displayDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function timelineTitle(entry: ApprovalTimelineEntry): string {
  if (entry.step_label) return entry.step_label;
  if (entry.kind === "submitted") return "申请提交";
  if (entry.kind === "withdrawn") return "申请撤回";
  return "流程完成";
}

function TimelineIcon({ entry }: { entry: ApprovalTimelineEntry }) {
  if (entry.status === "approved") return <CheckCircle2 aria-hidden="true" />;
  if (entry.status === "rejected" || entry.status === "cancelled") return <XCircle aria-hidden="true" />;
  if (entry.status === "pending" || entry.status === "waiting") return <Clock3 aria-hidden="true" />;
  return <CircleDashed aria-hidden="true" />;
}

function ProcurementTaskDetail({ subject }: SubjectRendererProps) {
  if (!subject.supported) return <UnsupportedSubjectDetail subject={subject} />;
  return (
    <div className="approval-procurement-detail">
      <section aria-labelledby="approval-business-facts">
        <header>
          <FileText aria-hidden="true" />
          <div>
            <span>PROCUREMENT FACTS</span>
            <h3 id="approval-business-facts">采购业务事实</h3>
          </div>
        </header>
        <dl className="approval-fact-grid">
          <div><dt>申请人</dt><dd>{subject.applicant.display_name}</dd></div>
          <div><dt>组织</dt><dd>{subject.organization.display_name}</dd></div>
          <div><dt>期望日期</dt><dd>{subject.needed_by_date}</dd></div>
          <div className="approval-purpose"><dt>用途说明</dt><dd>{subject.purpose}</dd></div>
          <div className="approval-total"><dt>权威总额</dt><dd>¥{subject.summary?.total ?? "—"} {subject.currency}</dd></div>
        </dl>
      </section>

      <section className="approval-line-items" aria-labelledby="approval-line-items-title">
        <header>
          <PackageOpen aria-hidden="true" />
          <h3 id="approval-line-items-title">采购明细</h3>
          <span>{String(subject.items.length).padStart(2, "0")} ITEMS</span>
        </header>
        <div className="approval-line-items-scroll">
          {subject.items.map((item, index) => (
            <article key={`${item.name}-${index}`}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <div>
                <strong>{item.name}</strong>
                <small>{CATEGORY_LABELS[item.category]}{item.specification ? ` · ${item.specification}` : ""}</small>
              </div>
              <p>{item.quantity} {item.unit} × ¥{item.unit_price}</p>
              <b>¥{item.subtotal}</b>
            </article>
          ))}
        </div>
      </section>

      <section className="approval-timeline-section" aria-labelledby="approval-timeline-title">
        <header>
          <span>TWO-LEVEL ROUTE</span>
          <h3 id="approval-timeline-title">审批时间线</h3>
        </header>
        <ol className="approval-timeline">
          {subject.timeline.map((entry, index) => (
            <li key={`${entry.occurred_at}-${entry.kind}-${index}`} data-status={entry.status}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <div className="approval-timeline-marker"><TimelineIcon entry={entry} /></div>
              <div>
                <div className="approval-timeline-heading">
                  <strong>{timelineTitle(entry)}</strong>
                  <em>{TIMELINE_STATUS_LABELS[entry.status]}</em>
                </div>
                <time>{displayDateTime(entry.occurred_at)}</time>
                {entry.actor_display_name && <small>处理人：{entry.actor_display_name}</small>}
                {entry.comment && <p>{entry.comment}</p>}
              </div>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

export function UnsupportedSubjectDetail({ subject: _subject }: SubjectRendererProps) {
  return (
    <section className="approval-unsupported" aria-label="不支持的业务类型">
      <CircleDashed aria-hidden="true" />
      <h3>不支持的业务类型</h3>
      <p>当前版本暂不支持此业务类型。</p>
    </section>
  );
}

export const subjectRenderers: Partial<Record<ApprovalSubjectType, SubjectRenderer>> = {
  procurement_request: ProcurementTaskDetail,
};

export function hasSubjectRenderer(subjectType: ApprovalSubjectType): boolean {
  return subjectRenderers[subjectType] !== undefined;
}

export function SubjectDetailRenderer({
  subjectType,
  subject,
}: SubjectRendererProps & { subjectType: ApprovalSubjectType }) {
  const Renderer = subject.supported ? subjectRenderers[subjectType] : undefined;
  return Renderer ? <Renderer subject={subject} /> : <UnsupportedSubjectDetail subject={subject} />;
}
