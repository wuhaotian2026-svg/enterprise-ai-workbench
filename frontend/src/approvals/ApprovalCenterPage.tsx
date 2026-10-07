import {
  AlertTriangle, ArrowLeft, ArrowRight, Check, CheckCircle2, Clock3, Filter, Inbox,
  RefreshCw, RotateCcw, ShieldCheck, X, XCircle,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { ApiError } from "../api/request";
import * as client from "./client";
import { hasSubjectRenderer, SubjectDetailRenderer } from "./subjectRenderers";
import type { ApprovalTaskDetail, ApprovalTaskPage, ApprovalTaskStatus, ApprovalTaskSummary } from "./types";
import "./approvals.css";

const PAGE_SIZE = 10;
const STATUS_LABELS: Record<ApprovalTaskStatus, string> = {
  waiting: "等待中", pending: "待处理", approved: "已批准", rejected: "已拒绝", cancelled: "已取消",
};
type QueueMode = "pending" | "completed";
type Filters = {
  mode: QueueMode; status: ApprovalTaskStatus; processKey: string;
  activatedFrom: string; activatedTo: string; offset: number;
};
type DecisionIntent = {
  kind: "approve" | "reject"; taskId: string; operationId: string;
  reason: string; ambiguous: boolean;
};

function sameDecisionIntent(left: DecisionIntent | null, right: DecisionIntent): left is DecisionIntent {
  return left !== null
    && left.kind === right.kind
    && left.taskId === right.taskId
    && left.operationId === right.operationId;
}

function subjectTitle(task: ApprovalTaskSummary): string {
  return task.subject.supported ? task.subject.title : "不支持的业务类型";
}
function subjectNumber(task: ApprovalTaskSummary): string {
  return task.subject.supported ? task.subject.request_number : "UNSUPPORTED";
}
function displayDateTime(value: string | null): string {
  if (!value) return "尚未激活";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(value));
}
function isAbort(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === "AbortError";
}
function failure(cause: unknown): { status: number; code: string } | null {
  return cause instanceof ApiError ? { status: cause.status, code: cause.code } : null;
}
function isAmbiguousFailure(cause: unknown): boolean {
  if (cause instanceof TypeError) return true;
  const result = failure(cause);
  if (result === null) return false;
  if (result.status >= 400 && result.status < 500) return result.status === 408 || result.status === 429;
  return result.code === "request_failed" || result.status >= 500;
}
function safeReadError(cause: unknown): string {
  const result = failure(cause);
  if (result?.status === 404) return "任务不存在或无权访问。";
  if (result?.status === 403) return "当前权限不足，无法查看此任务。";
  return "审批数据暂时无法读取，请稍后重试。";
}
function safeDecisionError(cause: unknown): string {
  const result = failure(cause);
  if (result?.status === 403) return "审批权限已变化，无法处理此任务。";
  if (result?.status === 404) return "任务不存在或无权访问。";
  if (result?.status === 422) return "任务当前不可处理，已结束本次操作。";
  return "审批操作未完成，请刷新后重试。";
}
function queryFromFilters(filters: Filters, signal: AbortSignal) {
  return {
    status: filters.status,
    processKey: filters.processKey || undefined,
    activatedFrom: filters.activatedFrom ? `${filters.activatedFrom}T00:00:00+08:00` : undefined,
    activatedTo: filters.activatedTo ? `${filters.activatedTo}T23:59:59+08:00` : undefined,
    offset: filters.offset,
    limit: PAGE_SIZE,
    signal,
  };
}
function StatusIcon({ status }: { status: ApprovalTaskStatus }) {
  if (status === "approved") return <CheckCircle2 aria-hidden="true" />;
  if (status === "rejected" || status === "cancelled") return <XCircle aria-hidden="true" />;
  return <Clock3 aria-hidden="true" />;
}

function DecisionDialog({ intent, busy, onReasonChange, onConfirm, onCancel }: {
  intent: DecisionIntent; busy: boolean; onReasonChange(value: string): void;
  onConfirm(): void; onCancel(): void;
}) {
  const dialogRef = useRef<HTMLElement | null>(null);
  const initialRef = useRef<HTMLButtonElement | HTMLTextAreaElement | null>(null);
  const onCancelRef = useRef(onCancel);
  const busyRef = useRef(busy);
  onCancelRef.current = onCancel;
  busyRef.current = busy;
  useEffect(() => {
    const dialog = dialogRef.current;
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog) return;
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), textarea:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ));
    initialRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (!busyRef.current) onCancelRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const elements = focusable();
      if (elements.length === 0) return;
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      if (trigger?.isConnected) trigger.focus();
    };
  }, []);
  const approving = intent.kind === "approve";
  const disabled = busy || (!approving && !intent.reason.trim());
  return (
    <div className="approval-modal-layer">
      <section ref={dialogRef} className={`approval-decision-dialog ${approving ? "is-approve" : "is-reject"}`}
        role="dialog" aria-modal="true" aria-label={approving ? "批准审批任务确认" : "拒绝审批任务确认"}>
        <header><span><AlertTriangle aria-hidden="true" />DECISION GATE</span><strong>{approving ? "APPROVE" : "REJECT"}</strong></header>
        <h2>{approving ? "确认批准当前审批任务？" : "说明拒绝当前任务的事实依据"}</h2>
        <p>系统不会依据金额或用途替你作出决定。提交后将以当前操作号执行同一笔明确意图。</p>
        {!approving && <label>拒绝理由<textarea ref={initialRef as RefObject<HTMLTextAreaElement | null>}
          value={intent.reason} maxLength={500} disabled={busy || intent.ambiguous}
          onChange={event => onReasonChange(event.target.value)} /></label>}
        {intent.ambiguous && <p className="approval-ambiguous" role="alert">上次请求结果暂时不明确。请使用同一操作号重试，不会创建新的审批意图。</p>}
        <div className="approval-decision-actions">
          <button type="button" disabled={busy} onClick={onCancel}><RotateCcw aria-hidden="true" />取消</button>
          <button ref={approving ? initialRef as RefObject<HTMLButtonElement | null> : undefined}
            type="button" disabled={disabled} onClick={onConfirm}>
            {approving ? <Check aria-hidden="true" /> : <X aria-hidden="true" />}
            {busy ? "处理中…" : intent.ambiguous ? "使用同一操作号重试" : approving ? "确认批准" : "确认拒绝"}
          </button>
        </div>
      </section>
    </div>
  );
}

export function ApprovalCenterPage() {
  const initialFilters: Filters = { mode: "pending", status: "pending", processKey: "", activatedFrom: "", activatedTo: "", offset: 0 };
  const [draft, setDraft] = useState<Filters>(initialFilters);
  const [filters, setFilters] = useState<Filters>(initialFilters);
  const [reloadToken, setReloadToken] = useState(0);
  const [tasks, setTasks] = useState<ApprovalTaskPage>({ items: [], offset: 0, limit: PAGE_SIZE, total: 0 });
  const [listLoading, setListLoading] = useState(true);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selected, setSelected] = useState<ApprovalTaskDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [intent, setIntent] = useState<DecisionIntent | null>(null);
  const [decisionBusy, setDecisionBusy] = useState(false);
  const listController = useRef<AbortController | null>(null);
  const detailController = useRef<AbortController | null>(null);
  const listSequence = useRef(0);
  const detailSequence = useRef(0);
  const detailDialogRef = useRef<HTMLElement | null>(null);
  const detailTriggerRef = useRef<HTMLElement | null>(null);
  const queueHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const intentRef = useRef<DecisionIntent | null>(null);
  const decisionBusyRef = useRef(false);
  const activeAttemptRef = useRef<DecisionIntent | null>(null);
  intentRef.current = intent;

  const closeDetail = useCallback((focusTarget?: HTMLElement | null) => {
    detailController.current?.abort();
    detailController.current = null;
    detailSequence.current += 1;
    setDetailLoading(false);
    setSelected(null);
    setSelectedTaskId(null);
    if (focusTarget?.isConnected) focusTarget.focus();
  }, []);

  const loadDetail = useCallback(async (taskId: string, preserveBanner = false, captureTrigger = true) => {
    if (captureTrigger && document.activeElement instanceof HTMLElement) detailTriggerRef.current = document.activeElement;
    detailController.current?.abort();
    const controller = new AbortController();
    detailController.current = controller;
    const sequence = ++detailSequence.current;
    setSelectedTaskId(taskId); setSelected(null); setDetailLoading(true);
    if (!preserveBanner) setBanner(null);
    try {
      const value = await client.getApprovalTask(taskId, controller.signal);
      if (sequence === detailSequence.current && !controller.signal.aborted) setSelected(value);
    } catch (cause) {
      if (!isAbort(cause) && sequence === detailSequence.current) setBanner(safeReadError(cause));
    } finally {
      if (sequence === detailSequence.current) setDetailLoading(false);
    }
  }, []);

  const loadList = useCallback(async () => {
    listController.current?.abort();
    const controller = new AbortController();
    listController.current = controller;
    const sequence = ++listSequence.current;
    setListLoading(true);
    try {
      const value = await client.listApprovalTasks(queryFromFilters(filters, controller.signal));
      if (sequence === listSequence.current && !controller.signal.aborted) setTasks(value);
    } catch (cause) {
      if (!isAbort(cause) && sequence === listSequence.current) setBanner(safeReadError(cause));
    } finally {
      if (sequence === listSequence.current) setListLoading(false);
    }
  }, [filters]);

  useEffect(() => { void loadList(); return () => listController.current?.abort(); }, [loadList, reloadToken]);
  useEffect(() => () => { listController.current?.abort(); detailController.current?.abort(); }, []);
  useEffect(() => {
    const dialog = detailDialogRef.current;
    if (!selected || !dialog) return;
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), textarea:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ));
    dialog.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (intentRef.current) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeDetail(detailTriggerRef.current);
        return;
      }
      if (event.key !== "Tab") return;
      const elements = focusable();
      if (elements.length === 0) { event.preventDefault(); dialog.focus(); return; }
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (!dialog.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [closeDetail, selected]);

  function changeMode(mode: QueueMode, focusTarget: HTMLElement) {
    const status: ApprovalTaskStatus = mode === "pending" ? "pending" : "approved";
    closeDetail(focusTarget);
    setDraft(current => ({ ...current, mode, status, offset: 0 }));
    setFilters(current => ({ ...current, mode, status, offset: 0 }));
  }
  function applyFilters(focusTarget: HTMLElement) {
    if (draft.activatedFrom && draft.activatedTo && draft.activatedFrom > draft.activatedTo) {
      setBanner("激活日期范围无效，请确认起止日期。");
      return;
    }
    closeDetail(focusTarget);
    setBanner(null);
    setFilters({ ...draft, offset: 0 });
  }
  function openDecision(kind: DecisionIntent["kind"]) {
    if (
      !selected
      || decisionBusyRef.current
      || intentRef.current
      || selected.task.status !== "pending"
      || !selected.task.subject.supported
      || !selected.subject.supported
      || !hasSubjectRenderer(selected.task.subject_type)
    ) return;
    setBanner(null);
    const next = { kind, taskId: selected.task.task_id, operationId: client.newApprovalOperationId(), reason: "", ambiguous: false };
    intentRef.current = next;
    setIntent(next);
  }
  function cancelDecision() {
    if (decisionBusyRef.current) return;
    intentRef.current = null;
    setIntent(null);
  }
  async function confirmDecision() {
    if (!intent || decisionBusyRef.current || (intent.kind === "reject" && !intent.reason.trim())) return;
    const attempt = intent;
    decisionBusyRef.current = true;
    activeAttemptRef.current = attempt;
    setDecisionBusy(true);
    setBanner(null);
    try {
      if (attempt.kind === "approve") await client.approveTask(attempt.taskId, attempt.operationId);
      else await client.rejectTask(attempt.taskId, attempt.reason.trim(), attempt.operationId);
      if (!sameDecisionIntent(intentRef.current, attempt)) return;
      intentRef.current = null;
      setIntent(null);
      closeDetail(queueHeadingRef.current);
      setBanner("审批决定已提交，队列已刷新。"); setReloadToken(value => value + 1);
    } catch (cause) {
      if (!sameDecisionIntent(intentRef.current, attempt)) return;
      if (failure(cause)?.status === 409) {
        const conflictedTaskId = attempt.taskId;
        intentRef.current = null;
        setIntent(null); setBanner("任务状态已变化，已重新加载权威状态。");
        setReloadToken(value => value + 1); void loadDetail(conflictedTaskId, true, false);
      } else if (isAmbiguousFailure(cause)) {
        const current = intentRef.current;
        if (sameDecisionIntent(current, attempt)) {
          const next = { ...current, ambiguous: true };
          intentRef.current = next;
          setIntent(next);
        }
      } else {
        intentRef.current = null;
        setIntent(null); setBanner(safeDecisionError(cause));
      }
    } finally {
      if (sameDecisionIntent(activeAttemptRef.current, attempt)) {
        activeAttemptRef.current = null;
        decisionBusyRef.current = false;
        setDecisionBusy(false);
      }
    }
  }
  const rangeStart = tasks.total === 0 ? 0 : tasks.offset + 1;
  const rangeEnd = Math.min(tasks.offset + tasks.items.length, tasks.total);

  return (
    <main className="approval-center-page" aria-labelledby="approval-center-heading">
      <header className="approval-hero">
        <div><span className="approval-kicker">UNIVERSAL APPROVAL DESK / 01</span><h1 id="approval-center-heading">审批中心</h1>
          <p>在同一权威队列中核对业务事实、两级流程与个人审批决定。</p></div>
        <div className="approval-hero-index" aria-label="审批边界"><ShieldCheck aria-hidden="true" /><span>HUMAN DECISION</span><strong>事实先行<br />不自动建议</strong></div>
      </header>
      {banner && <p className="approval-banner" role="alert">{banner}</p>}
      <section className="approval-toolbar" aria-label="审批队列筛选">
        <div className="approval-queue-tabs" aria-label="队列类型">
          <button type="button" className={draft.mode === "pending" ? "active" : ""} onClick={event => changeMode("pending", event.currentTarget)}><Inbox aria-hidden="true" />待处理</button>
          <button type="button" className={draft.mode === "completed" ? "active" : ""} onClick={event => changeMode("completed", event.currentTarget)}><CheckCircle2 aria-hidden="true" />已处理</button>
        </div>
        <div className="approval-filters">
          <label>任务状态<select value={draft.status} onChange={event => setDraft(current => ({ ...current, status: event.target.value as ApprovalTaskStatus }))}>
            {draft.mode === "pending" ? <><option value="pending">待处理</option><option value="waiting">等待中</option></>
              : <><option value="approved">已批准</option><option value="rejected">已拒绝</option><option value="cancelled">已取消</option></>}
          </select></label>
          <label>业务类型<select value={draft.processKey} onChange={event => setDraft(current => ({ ...current, processKey: event.target.value }))}>
            <option value="">全部业务</option><option value="procurement.request">采购申请</option>
          </select></label>
          <label>激活日期从<input type="date" value={draft.activatedFrom} onChange={event => setDraft(current => ({ ...current, activatedFrom: event.target.value }))} /></label>
          <label>激活日期至<input type="date" value={draft.activatedTo} onChange={event => setDraft(current => ({ ...current, activatedTo: event.target.value }))} /></label>
          <button type="button" onClick={event => applyFilters(event.currentTarget)}><Filter aria-hidden="true" />应用筛选</button>
        </div>
      </section>
      <section className="approval-desk">
        <section className="approval-queue" aria-labelledby="approval-queue-heading" aria-busy={listLoading}>
          <header><div><span>01 / QUEUE</span><h2 ref={queueHeadingRef} tabIndex={-1} id="approval-queue-heading">{filters.mode === "pending" ? "待处理队列" : "已处理记录"}</h2></div>
            <button type="button" onClick={() => setReloadToken(value => value + 1)}><RefreshCw aria-hidden="true" />刷新队列</button></header>
          {listLoading && <p role="status">正在读取审批队列…</p>}
          {!listLoading && tasks.items.length === 0 && <p className="approval-empty">当前筛选范围内没有审批任务。</p>}
          <div className="approval-task-list">
            {tasks.items.map((taskItem, index) => <button type="button" key={taskItem.task_id}
              className={selectedTaskId === taskItem.task_id ? "active" : ""}
              aria-label={`${subjectTitle(taskItem)}，${STATUS_LABELS[taskItem.status]}`} onClick={() => void loadDetail(taskItem.task_id)}>
              <span>{String(tasks.offset + index + 1).padStart(2, "0")}</span>
              <div><small>{subjectNumber(taskItem)}</small><strong>{subjectTitle(taskItem)}</strong><p>{taskItem.step_label}</p></div>
              <em data-status={taskItem.status}><StatusIcon status={taskItem.status} />{STATUS_LABELS[taskItem.status]}</em>
              <time>{displayDateTime(taskItem.activated_at)}</time>
            </button>)}
          </div>
          <footer className="approval-pagination"><span>第 {rangeStart}–{rangeEnd} 项，共 {tasks.total} 项</span><div>
            <button type="button" aria-label="上一页" disabled={filters.offset === 0} onClick={() => setFilters(current => ({ ...current, offset: Math.max(0, current.offset - PAGE_SIZE) }))}><ArrowLeft aria-hidden="true" /></button>
            <button type="button" aria-label="下一页" disabled={filters.offset + PAGE_SIZE >= tasks.total} onClick={() => setFilters(current => ({ ...current, offset: current.offset + PAGE_SIZE }))}><ArrowRight aria-hidden="true" /></button>
          </div></footer>
        </section>
        <section className="approval-detail-column" aria-label="审批详情">
          {!selectedTaskId && <div className="approval-detail-placeholder"><span>02 / DETAIL</span><Inbox aria-hidden="true" /><h2>选择一项任务核对事实</h2><p>详情只展示受信 DTO；未知业务类型不会显示原始载荷。</p></div>}
          {selectedTaskId && detailLoading && <p className="approval-detail-loading" role="status">正在读取任务详情…</p>}
          {selected && <article ref={detailDialogRef} tabIndex={-1} inert={intent ? true : undefined}
            className="approval-task-detail" role="dialog" aria-modal={intent ? undefined : "true"}
            aria-label={selected.task.subject.supported ? `采购申请 ${selected.task.subject.request_number}` : "审批任务详情"} aria-busy={detailLoading}>
            <header><div><span>{subjectNumber(selected.task)}</span><h2>{subjectTitle(selected.task)}</h2></div>
              <div className="approval-detail-heading-actions">
                <em data-status={selected.task.status}><StatusIcon status={selected.task.status} />{STATUS_LABELS[selected.task.status]}</em>
                <button type="button" aria-label="关闭审批详情" onClick={() => closeDetail(detailTriggerRef.current)}><X aria-hidden="true" />关闭</button>
              </div></header>
            <dl className="approval-task-facts">
              <div><dt>业务类型</dt><dd>{selected.task.subject.supported ? "采购申请" : "不支持的业务类型"}</dd></div>
              <div><dt>当前步骤</dt><dd>{selected.task.step_label}</dd></div>
              <div><dt>激活时间</dt><dd>{displayDateTime(selected.task.activated_at)}</dd></div>
              <div><dt>任务状态</dt><dd>{STATUS_LABELS[selected.task.status]}</dd></div>
            </dl>
            <SubjectDetailRenderer subjectType={selected.task.subject_type} subject={selected.subject} />
            {selected.task.status === "waiting" && <p className="approval-waiting-note"><Clock3 aria-hidden="true" />等待前序步骤完成</p>}
            {selected.task.status === "pending" && selected.task.subject.supported && selected.subject.supported
              && hasSubjectRenderer(selected.task.subject_type) && <footer className="approval-task-actions"><p>请依据业务事实和制度独立判断，系统不输出批准或拒绝建议。</p><div>
              <button type="button" onClick={() => openDecision("reject")}><XCircle aria-hidden="true" />拒绝</button>
              <button type="button" onClick={() => openDecision("approve")}><CheckCircle2 aria-hidden="true" />批准</button>
            </div></footer>}
          </article>}
        </section>
      </section>
      {intent && <DecisionDialog intent={intent} busy={decisionBusy}
        onReasonChange={reason => {
          const current = intentRef.current;
          if (!current || decisionBusyRef.current) return;
          const next = { ...current, reason };
          intentRef.current = next;
          setIntent(next);
        }}
        onConfirm={() => void confirmDecision()} onCancel={cancelDecision} />}
    </main>
  );
}
