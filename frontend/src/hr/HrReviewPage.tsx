import { Check, ClipboardCheck, UserRoundCheck, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/request";
import * as client from "./client";
import type { HrReviewRequest, LeaveRequestStatus } from "./types";
import "./hr.css";

const STATUS_LABELS: Record<LeaveRequestStatus, string> = {
  pending: "待审批",
  approved: "已批准",
  rejected: "已驳回",
  cancelled: "已撤销",
};

function isConflict(cause: unknown): boolean {
  return cause instanceof ApiError && cause.status === 409;
}

export function HrReviewPage() {
  const [status, setStatus] = useState<LeaveRequestStatus>("pending");
  const [queue, setQueue] = useState<HrReviewRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [banner, setBanner] = useState("");
  const [detail, setDetail] = useState<HrReviewRequest | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const operationIds = useRef(new Map<string, string>());

  function operationId(action: "approve" | "reject", requestId: string): string {
    const key = `${action}:${requestId}`;
    const value = operationIds.current.get(key) ?? client.newClientId();
    operationIds.current.set(key, value);
    return value;
  }

  const load = useCallback(async (value: LeaveRequestStatus) => {
    setLoading(true);
    setError("");
    try {
      setQueue(await client.listReviewQueue(value));
    } catch {
      setError("暂时无法读取审核队列，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(status); }, [load, status]);

  async function openDetail(item: HrReviewRequest) {
    setLoadingDetail(true);
    setBanner("");
    try {
      setDetail(await client.getReviewDetail(item.id));
      setRejecting(false);
      setReason("");
    } catch {
      setBanner("暂时无法读取申请详情。");
    } finally {
      setLoadingDetail(false);
    }
  }

  async function refreshAfterMutation(message: string) {
    setDetail(null);
    setRejecting(false);
    setReason("");
    await load(status);
    setBanner(message);
  }

  async function approve() {
    if (!detail || busy || detail.status !== "pending") return;
    setBusy(true);
    setBanner("");
    try {
      await client.approveLeaveRequest(detail.id, operationId("approve", detail.id));
      await refreshAfterMutation("申请已批准，审核队列已刷新。");
    } catch (cause) {
      if (isConflict(cause)) await refreshAfterMutation("申请状态已变化，已刷新真实状态。");
      else setBanner("批准未完成，请稍后重试。");
    } finally {
      setBusy(false);
    }
  }

  async function reject() {
    if (!detail || busy || detail.status !== "pending" || !reason.trim()) return;
    setBusy(true);
    setBanner("");
    try {
      await client.rejectLeaveRequest(detail.id, operationId("reject", detail.id), reason.trim());
      await refreshAfterMutation("申请已驳回，审核队列已刷新。");
    } catch (cause) {
      if (isConflict(cause)) await refreshAfterMutation("申请状态已变化，已刷新真实状态。");
      else setBanner("驳回未完成，请稍后重试。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="hr-operation-page hr-review-page">
      <header className="hr-operation-heading"><div><span className="eyebrow">HUMAN REVIEW / DETERMINISTIC</span><h1>HR 审核</h1></div><p><UserRoundCheck aria-hidden="true" />AI 不参与批准或驳回决定</p></header>
      <section className="hr-review-toolbar"><label>审核状态<select aria-label="审核状态" value={status} onChange={event => { setDetail(null); setStatus(event.target.value as LeaveRequestStatus); }}><option value="pending">待审批</option><option value="approved">已批准</option><option value="rejected">已驳回</option><option value="cancelled">已撤销</option></select></label><span>{queue.length.toString().padStart(2, "0")} ITEMS</span></section>
      {banner && <p className="hr-operation-banner" role="alert">{banner}</p>}
      {error && <p className="hr-operation-error" role="alert">{error}</p>}
      {loading && <p className="hr-operation-state" role="status">正在读取审核队列…</p>}
      {!loading && !error && queue.length === 0 && <p className="hr-operation-empty">{status === "pending" ? "当前没有待处理申请。" : "当前状态下没有申请。"}</p>}
      {!loading && !error && queue.length > 0 && <div className="hr-review-layout"><section className="hr-review-queue" aria-label="审核队列">{queue.map(item => <article key={item.id} className={detail?.id === item.id ? "selected" : ""}><span>{item.employee_number}</span><h2>{item.employee_display_name}</h2><p>{item.leave_type_name} · {item.start_date} — {item.end_date}</p><strong>{STATUS_LABELS[item.status]}</strong><button type="button" aria-label={`审核 ${item.request_number}`} onClick={() => void openDetail(item)}><ClipboardCheck aria-hidden="true" />查看审核</button></article>)}</section>
      <aside className="hr-review-detail" aria-label="申请审核详情">{loadingDetail && <p role="status">正在读取申请详情…</p>}{!loadingDetail && !detail && <div><span>SELECT A REQUEST</span><p>选择左侧申请后查看完整业务数据。</p></div>}{!loadingDetail && detail && <><header><span>{detail.request_number}</span><strong>{STATUS_LABELS[detail.status]}</strong></header><h2>{detail.employee_display_name}</h2><p>{detail.employee_number}</p><dl><div><dt>假期类型</dt><dd>{detail.leave_type_name}</dd></div><div><dt>日期</dt><dd>{detail.start_date} — {detail.end_date}</dd></div><div><dt>工作日</dt><dd>{Number(detail.workday_count)} 天</dd></div><div><dt>申请原因</dt><dd>{detail.reason || "—"}</dd></div></dl>{detail.status === "pending" && <div className="hr-review-actions"><button type="button" disabled={busy} onClick={() => void approve()}><Check aria-hidden="true" />批准申请</button><button type="button" disabled={busy} onClick={() => setRejecting(true)}><X aria-hidden="true" />驳回申请</button></div>}{rejecting && <div className="hr-rejection-form"><label htmlFor="hr-rejection-reason">驳回原因</label><textarea id="hr-rejection-reason" aria-label="驳回原因" value={reason} onChange={event => setReason(event.target.value)} maxLength={500} /><button type="button" disabled={busy || !reason.trim()} onClick={() => void reject()}>{busy ? "正在提交…" : "确认驳回"}</button></div>}</>}</aside></div>}
    </main>
  );
}
