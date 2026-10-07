import { CalendarDays, ClipboardList, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../api/request";
import * as client from "./client";
import { ConfirmationCard, type ConfirmationState } from "./ConfirmationCard";
import type { ConfirmationBlock, LeaveRequest, LeaveRequestStatus } from "./types";
import "./hr.css";

const STATUS_LABELS: Record<LeaveRequestStatus, string> = {
  pending: "待审批",
  approved: "已批准",
  rejected: "已驳回",
  cancelled: "已撤销",
};

type StatusFilter = LeaveRequestStatus | "all";

function errorCode(cause: unknown): string | undefined {
  return cause instanceof ApiError ? cause.code : undefined;
}

function isConflict(cause: unknown): boolean {
  return cause instanceof ApiError && cause.status === 409;
}

export function MyLeaveRequestsPage() {
  const [requests, setRequests] = useState<LeaveRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [banner, setBanner] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [confirmation, setConfirmation] = useState<ConfirmationBlock | null>(null);
  const [confirmationState, setConfirmationState] = useState<ConfirmationState>("active");
  const intentOperationIds = useRef(new Map<string, string>());
  const confirmOperationIds = useRef(new Map<string, string>());

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setRequests(await client.listLeaveRequests());
    } catch {
      setError("暂时无法读取申请记录，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const visible = useMemo(() => requests.filter(item => (
    (status === "all" || item.status === status)
    && (!fromDate || item.start_date >= fromDate)
    && (!toDate || item.end_date <= toDate)
  )), [fromDate, requests, status, toDate]);

  async function requestCancellation(item: LeaveRequest) {
    const operationId = intentOperationIds.current.get(item.id) ?? client.newClientId();
    intentOperationIds.current.set(item.id, operationId);
    setBanner("");
    try {
      const block = await client.createCancelIntent(item.id, operationId);
      setConfirmation(block);
      setConfirmationState(new Date(block.expires_at).getTime() <= Date.now() ? "expired" : "active");
    } catch (cause) {
      if (isConflict(cause)) {
        await load();
        setBanner("申请状态已变化，已刷新真实状态。");
      } else {
        setBanner("暂时无法生成撤销确认，没有执行任何撤销操作。");
      }
    }
  }

  async function confirmCancellation() {
    if (!confirmation || confirmationState === "busy") return;
    const operationId = confirmOperationIds.current.get(confirmation.confirmation_id) ?? client.newClientId();
    confirmOperationIds.current.set(confirmation.confirmation_id, operationId);
    setConfirmationState("busy");
    try {
      await client.confirmTool(confirmation.confirmation_id, operationId);
      setConfirmation(null);
      setBanner("申请已撤销，列表已刷新。");
      await load();
    } catch (cause) {
      if (errorCode(cause) === "tool_execution_non_retryable") {
        setConfirmationState("unrecoverable");
      } else if (isConflict(cause)) {
        setConfirmation(null);
        await load();
        setBanner("申请状态已变化，已刷新真实状态。");
      } else {
        setConfirmationState("failed");
        setBanner("撤销确认未完成，系统没有将申请标记为已撤销。");
      }
    }
  }

  async function dismissConfirmation() {
    if (!confirmation || confirmationState === "busy") return;
    setConfirmationState("busy");
    try {
      await client.cancelTool(confirmation.confirmation_id);
      setConfirmation(null);
      setBanner("已返回修改，本次没有撤销申请。");
    } catch {
      setConfirmationState("failed");
      setBanner("暂时无法关闭确认，请刷新后重试。");
    }
  }

  return (
    <main className="hr-operation-page">
      <header className="hr-operation-heading">
        <div><span className="eyebrow">EMPLOYEE SELF SERVICE / LEAVE</span><h1>我的申请</h1></div>
        <p><ClipboardList aria-hidden="true" />业务状态来自 HR 系统，待审批申请才可撤销</p>
      </header>
      <section className="hr-operation-filters" aria-label="申请筛选">
        <label>申请状态<select aria-label="申请状态" value={status} onChange={event => setStatus(event.target.value as StatusFilter)}><option value="all">全部状态</option><option value="pending">待审批</option><option value="approved">已批准</option><option value="rejected">已驳回</option><option value="cancelled">已撤销</option></select></label>
        <label>开始日期不早于<input aria-label="开始日期不早于" type="date" value={fromDate} onChange={event => setFromDate(event.target.value)} /></label>
        <label>结束日期不晚于<input aria-label="结束日期不晚于" type="date" value={toDate} onChange={event => setToDate(event.target.value)} /></label>
      </section>
      {banner && <p className="hr-operation-banner" role="alert">{banner}</p>}
      {error && <p className="hr-operation-error" role="alert">{error}</p>}
      {loading && <p className="hr-operation-state" role="status">正在读取申请记录…</p>}
      {!loading && !error && visible.length === 0 && <p className="hr-operation-empty">没有符合条件的请假申请。</p>}
      {!loading && !error && visible.length > 0 && <section className="hr-request-list" aria-label="请假申请列表">
        {visible.map(item => <article className={`hr-request-row status-${item.status}`} key={item.id}>
          <header><div><span>{item.request_number}</span><h2>{item.leave_type_name}</h2></div><strong><i aria-hidden="true" />{STATUS_LABELS[item.status]}</strong></header>
          <dl><div><dt>日期</dt><dd>{item.start_date} — {item.end_date}</dd></div><div><dt>工作日</dt><dd>{Number(item.workday_count)} 天</dd></div><div><dt>提交时间</dt><dd>{new Date(item.submitted_at).toLocaleString("zh-CN")}</dd></div><div><dt>原因</dt><dd>{item.reason || "—"}</dd></div></dl>
          {item.rejection_reason && <p>驳回原因：{item.rejection_reason}</p>}
          {item.status === "pending" && <button type="button" aria-label={`撤销 ${item.request_number}`} onClick={() => void requestCancellation(item)}><RotateCcw aria-hidden="true" />撤销申请</button>}
        </article>)}
      </section>}
      {confirmation && <div className="hr-operation-confirmation" role="dialog" aria-modal="true" aria-label="撤销申请确认"><ConfirmationCard block={confirmation} state={confirmationState} onConfirm={() => void confirmCancellation()} onCancel={() => void dismissConfirmation()} /></div>}
      <p className="hr-operation-footnote"><CalendarDays aria-hidden="true" />日期筛选在当前已加载的本人申请中执行。</p>
    </main>
  );
}
