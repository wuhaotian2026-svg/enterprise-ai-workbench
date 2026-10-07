import { ArrowLeft, NotebookPen, ShieldCheck, Trash2 } from "lucide-react";
import { useRef, useState } from "react";
import { ApiError } from "../api/request";
import * as client from "./client";
import type { JsonValue, ProcurementTurnResponse } from "./types";

type ConfirmationBlock = Extract<
  ProcurementTurnResponse["blocks"][number],
  { type: "confirmation" }
>;

type ProcurementAssistantTurnProps = {
  turn: ProcurementTurnResponse;
  now?: () => Date;
  onExecuted?: (resourceId: string) => void;
  onClearDraft?: () => void;
};

type ConfirmationState =
  | "active"
  | "busy"
  | "ambiguous"
  | "executed"
  | "cancelled"
  | "expired"
  | "failed";

function previewString(preview: Record<string, JsonValue>, key: string): string {
  const value = preview[key];
  return typeof value === "string" && value.trim() ? value.trim() : "未提供";
}

function previewCount(preview: Record<string, JsonValue>): string {
  const value = preview.item_count;
  return typeof value === "number" && Number.isInteger(value) && value >= 0
    ? String(value)
    : "未提供";
}

function displayExpiry(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "无法识别";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(timestamp));
}

function isAmbiguousFailure(cause: unknown): boolean {
  if (!(cause instanceof ApiError)) return true;
  return cause.code === "request_failed"
    || cause.status === 408
    || cause.status === 429
    || cause.status >= 500;
}

const PROCUREMENT_MISSING_LABELS: Record<string, string> = {
  title: "申请标题",
  purpose: "采购用途",
  needed_by_date: "需要日期",
  needed_by_year: "需要日期年份",
  currency: "币种",
  items: "采购明细",
};

const PROCUREMENT_ITEM_LEAF_LABELS: Record<string, string> = {
  item_name: "物品名称",
  quantity: "数量",
  unit: "计量单位",
  estimated_unit_price: "预估单价",
  category_code: "品类",
};

const ITEM_MISSING_PATH = /^items\[(\d+)\]\.([a-z_]+)$/;
const WHOLE_ITEM_MISSING_PATH = /^items\[(\d+)\]$/;
const UNKNOWN_MISSING_LABEL = "草稿中存在无法识别的信息状态，请清空草稿后重试";

function procurementMissingLabels(names: string[]): string[] {
  const labels: string[] = [];
  const grouped = new Map<number, string[]>();
  const addLabel = (label: string) => {
    if (!labels.includes(label)) labels.push(label);
  };
  for (const name of names) {
    const match = ITEM_MISSING_PATH.exec(name);
    if (!match) {
      const knownLabel = PROCUREMENT_MISSING_LABELS[name];
      if (knownLabel) {
        addLabel(knownLabel);
        continue;
      }
      const wholeItem = WHOLE_ITEM_MISSING_PATH.exec(name);
      if (wholeItem) {
        addLabel(`第 ${Number(wholeItem[1]) + 1} 项采购明细`);
        continue;
      }
      addLabel(UNKNOWN_MISSING_LABEL);
      continue;
    }
    const index = Number(match[1]);
    const leaf = PROCUREMENT_ITEM_LEAF_LABELS[match[2]];
    if (!leaf) {
      addLabel(UNKNOWN_MISSING_LABEL);
      continue;
    }
    const current = grouped.get(index) ?? [];
    if (!current.includes(leaf)) current.push(leaf);
    grouped.set(index, current);
  }
  for (const [index, leaves] of grouped) {
    labels.push(`第 ${index + 1} 项的${leaves.join("、")}`);
  }
  return labels;
}

function SubmitConfirmation({
  block,
  now,
  onExecuted,
}: {
  block: ConfirmationBlock;
  now: () => Date;
  onExecuted?: (resourceId: string) => void;
}) {
  const preview = block.preview;
  const initiallyExpired = Date.parse(block.expires_at) <= now().getTime();
  const [state, setState] = useState<ConfirmationState>(initiallyExpired ? "expired" : "active");
  const busyRef = useRef(false);
  const operationIdRef = useRef<string | null>(null);

  const terminal = state === "executed"
    || state === "cancelled"
    || state === "expired"
    || state === "failed";
  const disabled = state === "busy" || terminal;

  async function confirm() {
    if (busyRef.current || terminal) return;
    if (Date.parse(block.expires_at) <= now().getTime()) {
      setState("expired");
      return;
    }
    busyRef.current = true;
    setState("busy");
    operationIdRef.current ??= client.newProcurementOperationId();
    try {
      const result = await client.confirmProcurementSubmission(
        block.confirmation_id,
        operationIdRef.current,
      );
      setState("executed");
      onExecuted?.(result.resource_id);
    } catch (cause) {
      setState(isAmbiguousFailure(cause) ? "ambiguous" : "failed");
    } finally {
      busyRef.current = false;
    }
  }

  async function cancel() {
    if (busyRef.current || state !== "active") return;
    if (Date.parse(block.expires_at) <= now().getTime()) {
      setState("expired");
      return;
    }
    busyRef.current = true;
    setState("busy");
    try {
      await client.cancelProcurementConfirmation(block.confirmation_id);
      setState("cancelled");
    } catch {
      setState("failed");
    } finally {
      busyRef.current = false;
    }
  }

  const status = {
    active: "当前只是写操作提案，尚未创建采购申请。",
    busy: "正在确认提交，请勿重复操作。",
    ambiguous: "网络结果尚未确认，可以安全重试；重试会复用原操作号。",
    executed: "采购申请已创建，审批流程已启动。",
    cancelled: "已取消这次写操作提案，未创建采购申请。",
    expired: "此确认已过期，未执行任何提交。",
    failed: "提交未完成，未把失败状态显示为成功。",
  }[state];
  const statusRole = state === "ambiguous" || state === "expired" || state === "failed"
    ? "alert"
    : "status";

  return (
    <section
      className="procurement-assistant-confirmation"
      role="group"
      aria-label="采购 AI 提交确认"
      data-state={state}
    >
      <header>
        <ShieldCheck aria-hidden="true" />
        <div><span>WRITE PROPOSAL</span><strong>提交前需要你的明确确认</strong></div>
      </header>
      <dl>
        <div><dt>申请标题</dt><dd>{previewString(preview, "title")}</dd></div>
        <div><dt>采购用途</dt><dd>{previewString(preview, "purpose")}</dd></div>
        <div><dt>期望到货</dt><dd>{previewString(preview, "needed_by_date")}</dd></div>
        <div><dt>明细数量</dt><dd>{previewCount(preview)} 项</dd></div>
        <div className="procurement-assistant-confirmation-total">
          <dt>服务器权威总额</dt>
          <dd>¥{previewString(preview, "total")} {previewString(preview, "currency")}</dd>
        </div>
        <div><dt>确认有效期</dt><dd>{displayExpiry(block.expires_at)}</dd></div>
      </dl>
      <p role={statusRole}>{status}</p>
      <div className="procurement-assistant-confirmation-actions">
        <button
          type="button"
          aria-label="返回修改 AI 采购申请"
          disabled={disabled || state === "ambiguous"}
          onClick={() => void cancel()}
        >
          <ArrowLeft aria-hidden="true" />返回修改
        </button>
        <button
          type="button"
          aria-label={state === "ambiguous" ? "安全重试提交 AI 采购申请" : "确认提交 AI 采购申请"}
          disabled={disabled}
          onClick={() => void confirm()}
        >
          <ShieldCheck aria-hidden="true" />{state === "ambiguous" ? "安全重试" : "确认提交"}
        </button>
      </div>
    </section>
  );
}

export function ProcurementAssistantTurn({
  turn,
  now = () => new Date(),
  onExecuted,
  onClearDraft,
}: ProcurementAssistantTurnProps) {
  const primaryText = turn.blocks.find(block => block.type === "text")?.text || turn.text;
  const draft = turn.blocks.find(block => block.type === "assistant_draft");
  const draftItems = draft && Array.isArray(draft.fields.items) ? draft.fields.items : [];
  return (
    <article className="procurement-assistant-turn">
      {turn.request_content && <section className="procurement-assistant-user"><span>你提交的事项</span><p>{turn.request_content}</p></section>}
      <section className="procurement-assistant-response">
        <span>助手回复</span>
        {primaryText && <p>{primaryText}</p>}
        {draft && <section className="procurement-draft-card" aria-label="当前采购办事草稿"><header><NotebookPen aria-hidden="true" /><div><span>SESSION DRAFT</span><strong>已记录你明确提供的信息</strong></div></header>{draftItems.map((item, index) => typeof item === "object" && item !== null && !Array.isArray(item) ? <p key={index}><strong>{String(item.item_name ?? "采购明细")}</strong> · {String(item.quantity ?? "—")} {String(item.unit ?? "")} · 单价 ¥{String(item.estimated_unit_price ?? "—")}</p> : null)}<dl>{Object.entries(draft.fields).filter(([name]) => name !== "items").map(([name, value]) => <div key={name}><dt>{PROCUREMENT_MISSING_LABELS[name] ?? "已验证信息"}</dt><dd>{String(value)}</dd></div>)}</dl>{draft.missing_fields.length > 0 ? <p>还需补充：{procurementMissingLabels(draft.missing_fields).join("、")}</p> : <p>字段已齐全；如要办理，请明确发送“提交这份采购申请”。</p>}<button type="button" onClick={onClearDraft}><Trash2 aria-hidden="true" />清空草稿</button></section>}
      {turn.blocks.map((block, index) => (
        block.type === "confirmation"
          ? (
            <div key={`${block.confirmation_id}-${index}`}>
              {block.tool_name === "procurement.submit_request"
                ? <SubmitConfirmation block={block} now={now} onExecuted={onExecuted} />
                : null}
            </div>
          )
          : null
      ))}
      </section>
    </article>
  );
}
