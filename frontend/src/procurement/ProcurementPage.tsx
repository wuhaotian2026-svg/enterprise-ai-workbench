import {
  ArrowRight,
  Bot,
  CheckCircle2,
  CircleDashed,
  CircleX,
  ClipboardCheck,
  Copy,
  FileText,
  Plus,
  RefreshCcw,
  RotateCcw,
  Send,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { ApiError } from "../api/request";
import * as client from "./client";
import { ProcurementAssistantTurn } from "./ProcurementAssistantTurn";
import {
  ProcurementConfirmationCard,
  type ProcurementConfirmation,
} from "./ProcurementConfirmationCard";
import type {
  ProcurementCategoryCode,
  ProcurementRequestDetail,
  ProcurementRequestInput,
  ProcurementRequestItemInput,
  ProcurementRequestPreview,
  ProcurementRequestStatus,
  ProcurementRequestSummary,
  ProcurementTimelineEntry,
  ProcurementTurnResponse,
} from "./types";
import "./procurement.css";

type EditableItem = ProcurementRequestItemInput & { key: number };
type FormState = Omit<ProcurementRequestInput, "items"> & { items: EditableItem[] };
type PreviewSnapshot = { fingerprint: string; value: ProcurementRequestPreview };
type RequestFilters = {
  status: ProcurementRequestStatus | "";
  submittedFrom: string;
  submittedTo: string;
};

const STATUS_LABELS = {
  pending_manager: "待部门负责人审批",
  pending_procurement: "待采购专员复核",
  approved: "已完成",
  rejected: "已拒绝",
  cancelled: "已撤回",
} as const;

const CATEGORY_LABELS: Record<ProcurementCategoryCode, string> = {
  office_supplies: "办公用品",
  it_equipment: "IT 设备",
  software_service: "软件服务",
  professional_service: "专业服务",
  other: "其他",
};

const ERROR_MESSAGES: Record<string, string> = {
  procurement_profile_required: "当前账号尚未建立有效员工档案，无法提交采购申请。",
  procurement_manager_unavailable: "直属部门负责人暂不可用，请联系组织管理员维护负责人信息。",
  procurement_manager_capability_required: "直属部门负责人暂不具备采购审批权限，请联系管理员。",
  procurement_items_required: "请至少填写一条采购明细。",
  procurement_item_invalid: "采购明细格式不正确，请检查数量、单位与预估单价。",
  procurement_request_not_found: "未找到这笔申请，或当前账号无权查看。",
  procurement_request_state_conflict: "申请状态已发生变化，请以刷新后的状态为准。",
  procurement_request_conflict: "申请状态已发生变化，请以刷新后的状态为准。",
  operation_id_conflict: "本次操作号与已有操作不一致，请返回后重新发起。",
  procurement_response_invalid: "服务器返回了无法安全识别的数据，请稍后重试。",
};

const DECIMAL_PATTERN = /^(?:0|[1-9]\d{0,11})(?:\.\d{1,2})?$/;
const QUANTITY_PATTERN = /^(?:0\.(?:0[1-9]|[1-9]\d?)|[1-9]\d{0,9}(?:\.\d{1,2})?)$/;

function blankItem(key: number): EditableItem {
  return {
    key,
    category_code: "office_supplies",
    item_name: "",
    specification: null,
    quantity: "",
    unit: "",
    estimated_unit_price: "",
  };
}

function blankForm(): FormState {
  return {
    title: "",
    purpose: "",
    needed_by_date: "",
    currency: "CNY",
    items: [blankItem(1)],
  };
}

function requestInput(form: FormState): ProcurementRequestInput {
  return {
    title: form.title.trim(),
    purpose: form.purpose.trim(),
    needed_by_date: form.needed_by_date,
    currency: "CNY",
    items: form.items.map(({ key: _key, ...item }) => ({
      ...item,
      item_name: item.item_name.trim(),
      specification: item.specification?.trim() || null,
      unit: item.unit.trim(),
      quantity: item.quantity.trim(),
      estimated_unit_price: item.estimated_unit_price.trim(),
    })),
  };
}

function localToday(): string {
  const now = new Date();
  const year = String(now.getFullYear()).padStart(4, "0");
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function isCalendarDate(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day;
}

function isComplete(input: ProcurementRequestInput, today: string): boolean {
  return input.title.length > 0
    && input.title.length <= 160
    && input.purpose.length > 0
    && input.purpose.length <= 2000
    && isCalendarDate(input.needed_by_date)
    && input.needed_by_date >= today
    && input.items.length >= 1
    && input.items.length <= 50
    && input.items.every(item => item.item_name.length > 0
      && item.item_name.length <= 200
      && (item.specification === null || item.specification.length <= 500)
      && item.unit.length > 0
      && item.unit.length <= 40
      && QUANTITY_PATTERN.test(item.quantity)
      && DECIMAL_PATTERN.test(item.estimated_unit_price));
}

function apiFailure(cause: unknown): { status: number; code: string } | null {
  if (cause instanceof ApiError) return cause;
  if (typeof cause !== "object" || cause === null) return null;
  if (!("status" in cause) || typeof cause.status !== "number") return null;
  if (!("code" in cause) || typeof cause.code !== "string") return null;
  return { status: cause.status, code: cause.code };
}

function isConflict(cause: unknown): boolean {
  return apiFailure(cause)?.status === 409;
}

function isAmbiguousFailure(cause: unknown): boolean {
  const failure = apiFailure(cause);
  if (!failure) return true;
  return failure.code === "request_failed"
    || failure.status === 408
    || failure.status === 429
    || failure.status >= 500;
}

function isAbort(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === "AbortError";
}

function safeError(cause: unknown, fallback: string): string {
  const failure = apiFailure(cause);
  if (failure) return ERROR_MESSAGES[failure.code] ?? fallback;
  return fallback;
}

function displayDate(value: string): string {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

function timelinePresentation(entry: ProcurementTimelineEntry) {
  if (entry.kind === "submitted" && entry.status === "submitted") {
    return { title: "申请已提交", status: "已提交", actor: entry.actor_display_name ?? "申请人", Icon: FileText };
  }
  if (entry.kind === "decision" && entry.step_label && entry.action === "approve" && entry.status === "approved") {
    return { title: entry.step_label, status: "已批准", actor: entry.actor_display_name ?? "审批人", Icon: CheckCircle2 };
  }
  if (entry.kind === "decision" && entry.step_label && entry.action === "reject" && entry.status === "rejected") {
    return { title: entry.step_label, status: "已拒绝", actor: entry.actor_display_name ?? "审批人", Icon: CircleX };
  }
  if (entry.kind === "withdrawn" && entry.status === "cancelled") {
    return { title: "申请已撤回", status: "已撤回", actor: entry.actor_display_name ?? "申请人", Icon: RotateCcw };
  }
  if (entry.kind === "completed" && entry.status === "approved") {
    return { title: "申请已完成", status: "已完成", actor: "系统", Icon: ClipboardCheck };
  }
  if (entry.kind === "completed" && entry.status === "rejected") {
    return { title: "申请已拒绝并终止", status: "已拒绝", actor: "系统", Icon: CircleX };
  }
  if (entry.kind === "completed" && entry.status === "cancelled") {
    return { title: "申请已撤回", status: "已撤回", actor: "系统", Icon: RotateCcw };
  }
  return { title: "时间线记录无法识别", status: "无法识别", actor: "系统", Icon: CircleDashed };
}

export function ProcurementPage({ currentDate }: { currentDate?: string } = {}) {
  const today = currentDate ?? localToday();
  const [form, setForm] = useState<FormState>(blankForm);
  const [preview, setPreview] = useState<PreviewSnapshot | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [requests, setRequests] = useState<ProcurementRequestSummary[]>([]);
  const [requestPage, setRequestPage] = useState({ offset: 0, limit: 20, total: 0 });
  const [pageOffset, setPageOffset] = useState(0);
  const [listLoading, setListLoading] = useState(true);
  const [selected, setSelected] = useState<ProcurementRequestDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [banner, setBanner] = useState("");
  const [confirmation, setConfirmation] = useState<ProcurementConfirmation | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmAmbiguous, setConfirmAmbiguous] = useState(false);
  const submitOperationId = useRef<string | null>(null);
  const submitInput = useRef<ProcurementRequestInput | null>(null);
  const withdrawOperationIds = useRef(new Map<string, string>());
  const nextItemKey = useRef(2);
  const previewSequence = useRef(0);
  const listSequence = useRef(0);
  const detailSequence = useRef(0);
  const listController = useRef<AbortController | null>(null);
  const detailController = useRef<AbortController | null>(null);
  const [assistantDraft, setAssistantDraft] = useState("");
  const [assistantBusy, setAssistantBusy] = useState(false);
  const [assistantBanner, setAssistantBanner] = useState("");
  const [assistantTurn, setAssistantTurn] = useState<ProcurementTurnResponse | null>(null);
  const assistantIntent = useRef<{ text: string; turnId: string } | null>(null);
  const assistantInFlight = useRef(false);
  const conversationId = useRef<string | null>(null);
  const [draftFilters, setDraftFilters] = useState<RequestFilters>({ status: "", submittedFrom: "", submittedTo: "" });
  const [appliedFilters, setAppliedFilters] = useState<RequestFilters>({ status: "", submittedFrom: "", submittedTo: "" });
  const [filterError, setFilterError] = useState("");
  const detailDialogRef = useRef<HTMLElement | null>(null);
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null);

  const canonicalInput = requestInput(form);
  const inputFingerprint = JSON.stringify(canonicalInput);
  const activePreview = preview?.fingerprint === inputFingerprint ? preview.value : null;

  const loadRequests = useCallback(async () => {
    const sequence = ++listSequence.current;
    listController.current?.abort();
    const controller = new AbortController();
    listController.current = controller;
    setListLoading(true);
    try {
      const page = await client.listProcurementRequests({
        status: appliedFilters.status || undefined,
        submittedFrom: appliedFilters.submittedFrom || undefined,
        submittedTo: appliedFilters.submittedTo || undefined,
        offset: pageOffset,
        limit: 20,
        signal: controller.signal,
      });
      if (sequence === listSequence.current) {
        setRequests(page.items);
        setRequestPage({ offset: page.offset, limit: page.limit, total: page.total });
      }
    } catch (cause) {
      if (sequence === listSequence.current && !isAbort(cause)) {
        setBanner(safeError(cause, "暂时无法读取采购申请，请稍后重试。"));
      }
    } finally {
      if (sequence === listSequence.current) setListLoading(false);
    }
  }, [appliedFilters, pageOffset]);

  const openDetail = useCallback(async (id: string) => {
    const sequence = ++detailSequence.current;
    detailController.current?.abort();
    const controller = new AbortController();
    detailController.current = controller;
    setDetailLoading(true);
    try {
      const detail = await client.getProcurementRequest(id, controller.signal);
      if (sequence === detailSequence.current) setSelected(detail);
    } catch (cause) {
      if (sequence === detailSequence.current && !isAbort(cause)) {
        setBanner(safeError(cause, "暂时无法读取采购申请详情。"));
      }
    } finally {
      if (sequence === detailSequence.current) setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRequests();
    return () => listController.current?.abort();
  }, [loadRequests]);

  useEffect(() => () => detailController.current?.abort(), []);

  useEffect(() => {
    let active = true;
    void client.listProcurementConversations().then(async conversations => {
      if (!active || !conversations[0]) return;
      conversationId.current = conversations[0].id;
      const detail = await client.getProcurementConversation(conversations[0].id);
      if (!active) return;
      const latest = [...detail.turns].reverse().find(turn => turn.role === "assistant");
      if (latest) setAssistantTurn({ ...latest, replayed: false });
    }).catch(() => undefined);
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!selected || !detailDialogRef.current) return;
    const dialog = detailDialogRef.current;
    const trigger = detailTriggerRef.current;
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ));
    focusable()[0]?.focus();
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (confirmation) return;
      if (event.key === "Escape") {
        event.preventDefault();
        setSelected(null);
        return;
      }
      if (event.key !== "Tab") return;
      const elements = focusable();
      if (elements.length === 0) return;
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      trigger?.focus();
    };
  }, [confirmation, selected]);

  useEffect(() => {
    const input = requestInput(form);
    const sequence = ++previewSequence.current;
    if (!isComplete(input, today)) {
      setPreview(null);
      setPreviewError("");
      setPreviewing(false);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setPreviewing(true);
      setPreviewError("");
      void client.previewProcurementRequest(input, controller.signal)
        .then(result => {
          if (sequence === previewSequence.current) setPreview({ fingerprint: JSON.stringify(input), value: result });
        })
        .catch(cause => {
          if (sequence === previewSequence.current && !isAbort(cause)) {
            setPreview(null);
            setPreviewError(safeError(cause, "暂时无法计算权威金额，请检查明细或稍后重试。"));
          }
        })
        .finally(() => {
          if (sequence === previewSequence.current) setPreviewing(false);
        });
    }, 180);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [form, today]);

  function updateItem(key: number, field: keyof ProcurementRequestItemInput, value: string) {
    setForm(current => ({
      ...current,
      items: current.items.map(item => item.key === key
        ? { ...item, [field]: field === "specification" ? value || null : value }
        : item),
    }));
  }

  function addItem() {
    setForm(current => current.items.length >= 50 ? current : {
      ...current,
      items: [...current.items, blankItem(nextItemKey.current++)],
    });
  }

  function removeItem(key: number) {
    setForm(current => current.items.length === 1 ? current : {
      ...current,
      items: current.items.filter(item => item.key !== key),
    });
  }

  function openSubmitConfirmation() {
    const input = requestInput(form);
    const fingerprint = JSON.stringify(input);
    if (!isComplete(input, today)) {
      setBanner("请完整填写主信息与所有采购明细。数量和单价最多保留两位小数。");
      return;
    }
    if (!activePreview || preview?.fingerprint !== fingerprint) {
      setBanner("请等待服务器完成权威金额预览后再提交。没有创建任何申请。");
      return;
    }
    submitOperationId.current = client.newProcurementOperationId();
    submitInput.current = input;
    setConfirmAmbiguous(false);
    setBanner("");
    setConfirmation({ kind: "submit", title: input.title, total: activePreview.total });
  }

  async function submit() {
    if (!confirmation || confirmation.kind !== "submit" || confirmBusy || !submitOperationId.current || !submitInput.current) return;
    setConfirmBusy(true);
    setBanner("");
    try {
      const result = await client.submitProcurementRequest(submitInput.current, submitOperationId.current);
      setBanner(`申请 ${result.request_number} 已提交。服务器确认总额：¥${result.total}`);
      submitOperationId.current = null;
      submitInput.current = null;
      setConfirmation(null);
      setConfirmAmbiguous(false);
      setForm(blankForm());
      setPreview(null);
      await loadRequests();
    } catch (cause) {
      if (isConflict(cause)) {
        submitOperationId.current = null;
        submitInput.current = null;
        setConfirmation(null);
        setConfirmAmbiguous(false);
        await loadRequests();
        setBanner("状态已变化，已刷新服务器中的真实状态。");
      } else if (apiFailure(cause)) {
        if (isAmbiguousFailure(cause)) {
          setConfirmAmbiguous(true);
          setBanner("请求结果暂时不明确。内容和操作号均已保留，可以安全重试。");
          return;
        }
        setBanner(safeError(cause, "提交未完成，没有将申请标记为成功。"));
        submitOperationId.current = null;
        submitInput.current = null;
        setConfirmation(null);
        setConfirmAmbiguous(false);
      } else {
        setConfirmAmbiguous(true);
        setBanner("请求结果暂时不明确。内容和操作号均已保留，可以安全重试。");
      }
    } finally {
      setConfirmBusy(false);
    }
  }

  function requestWithdraw(detail: ProcurementRequestDetail) {
    const operationId = client.newProcurementOperationId();
    withdrawOperationIds.current.set(detail.id, operationId);
    setConfirmAmbiguous(false);
    setConfirmation({
      kind: "withdraw",
      requestId: detail.id,
      requestNumber: detail.summary.request_number,
      title: detail.summary.title,
      total: detail.summary.total,
    });
  }

  async function withdraw() {
    if (!confirmation || confirmation.kind !== "withdraw" || confirmBusy) return;
    const { requestId, requestNumber } = confirmation;
    const operationId = withdrawOperationIds.current.get(requestId);
    if (!operationId) return;
    setConfirmBusy(true);
    try {
      await client.withdrawProcurementRequest(requestId, operationId);
      withdrawOperationIds.current.delete(requestId);
      setConfirmation(null);
      setConfirmAmbiguous(false);
      setBanner(`申请 ${requestNumber} 已撤回。`);
      await Promise.all([loadRequests(), openDetail(requestId)]);
    } catch (cause) {
      if (isConflict(cause)) {
        withdrawOperationIds.current.delete(requestId);
        setConfirmation(null);
        setConfirmAmbiguous(false);
        await Promise.all([loadRequests(), openDetail(requestId)]);
        setBanner("状态已变化，已刷新服务器中的真实状态。");
      } else if (apiFailure(cause)) {
        if (isAmbiguousFailure(cause)) {
          setConfirmAmbiguous(true);
          setBanner("撤回结果暂时不明确。操作号已保留，可以安全重试。");
          return;
        }
        withdrawOperationIds.current.delete(requestId);
        setConfirmation(null);
        setConfirmAmbiguous(false);
        setBanner(safeError(cause, "撤回未完成，申请状态没有被标记为已撤回。"));
      } else {
        setConfirmAmbiguous(true);
        setBanner("撤回结果暂时不明确。操作号已保留，可以安全重试。");
      }
    } finally {
      setConfirmBusy(false);
    }
  }

  function cancelConfirmation() {
    if (confirmBusy) return;
    if (confirmation?.kind === "submit") {
      submitOperationId.current = null;
      submitInput.current = null;
    }
    if (confirmation?.kind === "withdraw") withdrawOperationIds.current.delete(confirmation.requestId);
    setConfirmation(null);
    setConfirmAmbiguous(false);
  }

  function copyRequest(detail: ProcurementRequestDetail) {
    nextItemKey.current = detail.items.length + 1;
    setForm({
      title: detail.summary.title,
      purpose: detail.purpose,
      needed_by_date: detail.needed_by_date,
      currency: detail.currency,
      items: detail.items.map((item, index) => ({
        key: index + 1,
        category_code: item.category,
        item_name: item.name,
        specification: item.specification,
        quantity: item.quantity,
        unit: item.unit,
        estimated_unit_price: item.unit_price,
      })),
    });
    submitOperationId.current = null;
    submitInput.current = null;
    setSelected(null);
    setConfirmation(null);
    setBanner("已复制允许复用的业务字段。提交后将生成新的申请编号和审批实例。");
    window.setTimeout(() => document.getElementById("procurement-form-title")?.focus(), 0);
  }

  async function sendAssistantTurn(value = assistantDraft) {
    const text = value.trim();
    if (!text || assistantInFlight.current) return;
    assistantInFlight.current = true;
    setAssistantBusy(true);
    setAssistantBanner("");
    const intent = assistantIntent.current?.text === text
      ? assistantIntent.current
      : { text, turnId: client.newProcurementOperationId() };
    assistantIntent.current = intent;
    try {
      if (!conversationId.current) {
        conversationId.current = (await client.createProcurementConversation("采购办事辅助")).id;
      }
      const turn = await client.sendProcurementTurn(conversationId.current, text, intent.turnId);
      setAssistantTurn(turn);
      setAssistantDraft("");
      assistantIntent.current = null;
    } catch (cause) {
      if (isConflict(cause)) {
        setAssistantBanner("会话状态已变化，请重新描述这项采购事项。");
        assistantIntent.current = null;
      } else if (apiFailure(cause)) {
        if (isAmbiguousFailure(cause)) {
          setAssistantBanner("本次请求未完成。内容已保留，可以安全重试。");
          return;
        }
        setAssistantBanner(safeError(cause, "采购 AI 助手暂时无法处理这项请求。"));
        assistantIntent.current = null;
      } else {
        setAssistantBanner("本次请求未完成。内容已保留，可以安全重试。");
      }
    } finally {
      assistantInFlight.current = false;
      setAssistantBusy(false);
    }
  }

  function onAssistantComposerKeyDown(
    event: ReactKeyboardEvent<HTMLTextAreaElement>,
  ) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void sendAssistantTurn();
  }

  const running = selected?.summary.status === "pending_manager"
    || selected?.summary.status === "pending_procurement";
  const statusCounts = requests.reduce<Record<ProcurementRequestStatus, number>>((counts, request) => {
    counts[request.status] += 1;
    return counts;
  }, { pending_manager: 0, pending_procurement: 0, approved: 0, rejected: 0, cancelled: 0 });
  const hasPreviousPage = requestPage.offset > 0;
  const hasNextPage = requestPage.offset + requests.length < requestPage.total;

  return (
    <main className="procurement-page">
      <header className="procurement-hero">
        <div><span className="eyebrow">PROCUREMENT / CONTROLLED WORKFLOW</span><h1>采购申请</h1></div>
        <p><ShieldCheck aria-hidden="true" />表单与审批可独立于 AI 完成；金额由服务器以 Decimal 权威计算</p>
      </header>

      {banner && <p className="procurement-banner" role="alert">{banner}</p>}

      <section className="procurement-overview" aria-label="采购工作概览">
        <div><span>01</span><strong>{requests.filter(item => item.status.startsWith("pending_")).length}</strong><small>我的待审批申请</small></div>
        <div><span>02</span><strong>{requests[0]?.request_number ?? "—"}</strong><small>最近申请</small></div>
        <div><span>03</span><strong>2 LEVELS</strong><small>固定审批链</small></div>
      </section>

      <div className="procurement-workspace">
        <section className="procurement-form-panel" aria-labelledby="procurement-form-heading">
          <header><span>APPLICATION / 01</span><h2 id="procurement-form-heading">新建采购申请</h2><p>业务申请与 AI 助手相互独立。所有明细完整后才会请求服务器金额预览。</p></header>
          <form onSubmit={event => { event.preventDefault(); openSubmitConfirmation(); }}>
            <div className="procurement-main-fields">
              <label htmlFor="procurement-form-title">申请标题<input id="procurement-form-title" value={form.title} maxLength={160} onChange={event => setForm(current => ({ ...current, title: event.target.value }))} /></label>
              <label>期望到货日期<input aria-label="期望到货日期" type="date" min={today} value={form.needed_by_date} onChange={event => setForm(current => ({ ...current, needed_by_date: event.target.value }))} /></label>
              <label className="procurement-purpose">采购用途<textarea aria-label="采购用途" rows={4} maxLength={2000} value={form.purpose} onChange={event => setForm(current => ({ ...current, purpose: event.target.value }))} /></label>
            </div>

            <fieldset className="procurement-items-fieldset">
              <legend>采购明细 · {form.items.length}/50</legend>
              {form.items.map((item, index) => (
                <fieldset className="procurement-item" aria-label={`采购明细 ${index + 1}`} key={item.key}>
                  <legend><span>{String(index + 1).padStart(2, "0")}</span> ITEM</legend>
                  <label>品类<select aria-label={`品类 ${index + 1}`} value={item.category_code} onChange={event => updateItem(item.key, "category_code", event.target.value)}>{Object.entries(CATEGORY_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
                  <label>物品名称<input aria-label={`物品名称 ${index + 1}`} maxLength={200} value={item.item_name} onChange={event => updateItem(item.key, "item_name", event.target.value)} /></label>
                  <label>规格说明<input aria-label={`规格说明 ${index + 1}`} maxLength={500} value={item.specification ?? ""} onChange={event => updateItem(item.key, "specification", event.target.value)} /></label>
                  <label>数量<input aria-label={`数量 ${index + 1}`} inputMode="decimal" value={item.quantity} onChange={event => updateItem(item.key, "quantity", event.target.value)} /></label>
                  <label>单位<input aria-label={`单位 ${index + 1}`} maxLength={40} value={item.unit} onChange={event => updateItem(item.key, "unit", event.target.value)} /></label>
                  <label>预估单价<input aria-label={`预估单价 ${index + 1}`} inputMode="decimal" value={item.estimated_unit_price} onChange={event => updateItem(item.key, "estimated_unit_price", event.target.value)} /></label>
                  <div className="procurement-item-total"><span>服务器小计</span><strong>{activePreview?.subtotals[index] ? `¥${activePreview.subtotals[index]}` : "待完整填写"}</strong></div>
                  <button type="button" aria-label={`删除采购明细 ${index + 1}`} disabled={form.items.length === 1} onClick={() => removeItem(item.key)}><Trash2 aria-hidden="true" />删除</button>
                </fieldset>
              ))}
              <button className="procurement-add-item" type="button" disabled={form.items.length >= 50} onClick={addItem}><Plus aria-hidden="true" />添加采购明细</button>
            </fieldset>

            <section className="procurement-server-preview" aria-live="polite">
              <div><span>SERVER DECIMAL PREVIEW</span><small>不在浏览器中进行浮点乘加</small></div>
              <strong>{previewing ? "计算中…" : activePreview ? `服务器权威总额 ¥${activePreview.total}` : "完整填写后计算"}</strong>
              {previewError && <p role="alert">{previewError}</p>}
            </section>

            <ol className="procurement-route" aria-label="两级审批路线">
              <li><span>01</span><div><strong>直属部门负责人审批</strong><small>确认部门需要与申请内容</small></div><ArrowRight aria-hidden="true" /></li>
              <li><span>02</span><div><strong>采购专员复核</strong><small>按组织授权范围复核</small></div><CheckCircle2 aria-hidden="true" /></li>
            </ol>
            <button className="procurement-submit" type="submit" disabled={!activePreview || previewing}><ClipboardCheck aria-hidden="true" />检查并提交</button>
          </form>
        </section>

        <aside className="procurement-assistant" aria-label="采购 AI 助手">
          <header><Bot aria-hidden="true" /><div><span>ASSIST / OPTIONAL</span><h2>采购 AI 助手</h2></div></header>
          <p>可帮助梳理字段、查询制度或读取本人申请。模型不能直接提交、撤回或替审批人做决定。</p>
          {assistantBanner && <p className="procurement-assistant-alert" role="alert">{assistantBanner}</p>}
          {assistantTurn && (
            <ProcurementAssistantTurn
              key={assistantTurn.id}
              turn={assistantTurn}
              onExecuted={() => void loadRequests()}
              onClearDraft={() => void sendAssistantTurn("清空草稿")}
            />
          )}
          <form onSubmit={event => { event.preventDefault(); void sendAssistantTurn(); }}>
            <label>向采购 AI 助手说明事项<textarea aria-label="向采购 AI 助手说明事项" rows={5} value={assistantDraft} disabled={assistantBusy} onChange={event => setAssistantDraft(event.target.value)} onKeyDown={onAssistantComposerKeyDown} /></label>
            <button type="submit" aria-label={assistantIntent.current ? "重试采购 AI 请求" : "发送给采购 AI 助手"} disabled={assistantBusy || !assistantDraft.trim()}>{assistantBusy ? "处理中…" : assistantIntent.current ? "安全重试" : "发送"}<Send aria-hidden="true" /></button>
          </form>
          <small>Enter 发送 · Shift+Enter 换行</small>
          <small>AI 不可用时，左侧确定性表单仍可完整办理。</small>
        </aside>
      </div>

      <section className="procurement-records" aria-labelledby="procurement-records-heading">
        <header><div><span>MY REQUESTS / 02</span><h2 id="procurement-records-heading">我的申请</h2></div><button type="button" onClick={() => void loadRequests()}><RefreshCcw aria-hidden="true" />刷新</button></header>
        <form className="procurement-record-filters" onSubmit={event => {
          event.preventDefault();
          if (draftFilters.submittedFrom && draftFilters.submittedTo && draftFilters.submittedFrom > draftFilters.submittedTo) {
            setFilterError("开始日期不能晚于结束日期，请修正后重新应用筛选。");
            return;
          }
          setFilterError("");
          setPageOffset(0);
          setAppliedFilters(draftFilters);
        }}>
          <label>申请状态<select aria-label="申请状态" value={draftFilters.status} onChange={event => { setFilterError(""); setDraftFilters(current => ({ ...current, status: event.target.value as RequestFilters["status"] })); }}><option value="">全部状态</option>{Object.entries(STATUS_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <label>提交日期从<input aria-label="提交日期从" type="date" value={draftFilters.submittedFrom} onChange={event => { setFilterError(""); setDraftFilters(current => ({ ...current, submittedFrom: event.target.value })); }} /></label>
          <label>提交日期至<input aria-label="提交日期至" type="date" value={draftFilters.submittedTo} onChange={event => { setFilterError(""); setDraftFilters(current => ({ ...current, submittedTo: event.target.value })); }} /></label>
          <button type="submit">应用筛选</button>
        </form>
        {filterError && <p className="procurement-filter-error" role="alert">{filterError}</p>}
        <div className="procurement-page-distribution"><strong>当前页状态分布</strong><p>待负责人 {statusCounts.pending_manager} · 待采购 {statusCounts.pending_procurement} · 已完成 {statusCounts.approved} · 已拒绝 {statusCounts.rejected} · 已撤回 {statusCounts.cancelled}</p></div>
        {listLoading && <p role="status">正在读取采购申请…</p>}
        {!listLoading && requests.length === 0 && <p>还没有采购申请。</p>}
        {!listLoading && requests.map(item => (
          <article className={`procurement-record status-${item.status}`} key={item.id}>
            <div><span>{item.request_number}</span><h3>{item.title}</h3></div>
            <strong><i aria-hidden="true" />{STATUS_LABELS[item.status]}</strong>
            <dl><div><dt>服务器总额</dt><dd>¥{item.total}</dd></div><div><dt>提交时间</dt><dd>{displayDate(item.submitted_at)}</dd></div></dl>
            <button type="button" aria-label={`查看 ${item.request_number}`} onClick={event => { detailTriggerRef.current = event.currentTarget; void openDetail(item.id); }}><FileText aria-hidden="true" />查看详情</button>
          </article>
        ))}
        <nav className="procurement-pagination" aria-label="采购申请分页">
          <span>共 {requestPage.total} 条 · 第 {Math.floor(requestPage.offset / requestPage.limit) + 1} 页</span>
          <div><button type="button" disabled={listLoading || !hasPreviousPage} onClick={() => setPageOffset(current => Math.max(0, current - 20))}>上一页</button><button type="button" disabled={listLoading || !hasNextPage} onClick={() => setPageOffset(current => current + 20)}>下一页</button></div>
        </nav>
      </section>

      {selected && (
        <div className="procurement-detail-overlay" aria-hidden={confirmation ? "true" : undefined} inert={confirmation ? true : undefined}>
          <section ref={detailDialogRef} className="procurement-detail" role="dialog" aria-modal={confirmation ? undefined : "true"} aria-label={`采购申请 ${selected.summary.request_number}`} aria-busy={detailLoading}>
            <header><div><span>{selected.summary.request_number}</span><h2>{selected.summary.title}</h2></div><button type="button" aria-label="关闭采购详情" onClick={() => setSelected(null)}>关闭</button></header>
            <strong className={`procurement-detail-status status-${selected.summary.status}`}><i aria-hidden="true" />{STATUS_LABELS[selected.summary.status]}</strong>
            <dl><div><dt>申请人</dt><dd>{selected.applicant.display_name}</dd></div><div><dt>期望到货</dt><dd>{selected.needed_by_date}</dd></div><div><dt>服务器总额</dt><dd>¥{selected.summary.total}</dd></div><div><dt>用途</dt><dd>{selected.purpose}</dd></div></dl>
            <section className="procurement-detail-items" aria-label="申请明细"><h3>申请明细</h3>{selected.items.map((item, index) => <article key={`${item.name}-${index}`}><span>{CATEGORY_LABELS[item.category]}</span><strong>{item.name}</strong><p>{item.specification || "无规格说明"}</p><small>{item.quantity} {item.unit} × ¥{item.unit_price} · 小计 ¥{item.subtotal}</small></article>)}</section>
            <ol className="procurement-timeline" aria-label="审批时间线">{selected.timeline.map((entry, index) => {
              const presentation = timelinePresentation(entry);
              const StatusIcon = presentation.Icon;
              return <li key={`${entry.kind}-${entry.occurred_at}-${index}`}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div>
                  <strong>{presentation.title}</strong>
                  <small>{presentation.actor} · {displayDate(entry.occurred_at)}</small>
                  <span className="procurement-timeline-status"><StatusIcon aria-hidden="true" />{presentation.status}</span>
                  {entry.comment && <p>{entry.comment}</p>}
                </div>
              </li>;
            })}</ol>
            <div className="procurement-detail-actions">
              {running && <button type="button" aria-label={`撤回 ${selected.summary.request_number}`} onClick={() => requestWithdraw(selected)}><RotateCcw aria-hidden="true" />撤回申请</button>}
              <button type="button" aria-label={`复制 ${selected.summary.request_number} 新建`} onClick={() => copyRequest(selected)}><Copy aria-hidden="true" />复制新建</button>
            </div>
          </section>
        </div>
      )}

      {confirmation && <div className="procurement-confirmation-overlay"><ProcurementConfirmationCard confirmation={confirmation} busy={confirmBusy} ambiguous={confirmAmbiguous} onCancel={cancelConfirmation} onConfirm={() => void (confirmation.kind === "submit" ? submit() : withdraw())} /></div>}
    </main>
  );
}
