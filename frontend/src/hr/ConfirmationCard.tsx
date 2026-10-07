import { AlertTriangle, Check, RotateCcw, X } from "lucide-react";
import type { ConfirmationBlock } from "./types";

export type ConfirmationState = "active" | "busy" | "expired" | "cancelled" | "failed" | "unrecoverable";

const FIELD_LABELS: Record<string, string> = {
  leave_type_code: "假期类型",
  leave_type_name: "假期名称",
  start_date: "开始日期",
  end_date: "结束日期",
  workday_count: "工作日数",
  available_before: "提交前可用余额",
  available_after: "提交后可用余额",
  reason: "请假原因",
  request_number: "申请编号",
  status: "当前状态",
};

function formatValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (key === "leave_type_code" && value === "annual") return "年假";
  if (key === "leave_type_code" && value === "compensatory") return "调休";
  if (key === "status" && value === "pending") return "待审批";
  if (["workday_count", "available_before", "available_after"].includes(key)) {
    const days = Number(value);
    if (Number.isFinite(days)) return `${days.toLocaleString("zh-CN")} 天`;
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

export function ConfirmationCard({
  block,
  state,
  onConfirm,
  onCancel,
}: {
  block: ConfirmationBlock;
  state: ConfirmationState;
  onConfirm(): void;
  onCancel(): void;
}) {
  const cancellation = block.tool_name === "hr.cancel_leave_request";
  const cancelUnavailable = state === "busy" || state === "expired" || state === "cancelled";
  const confirmUnavailable = cancelUnavailable || state === "unrecoverable";
  const visualState = state === "unrecoverable" ? "failed" : state;
  return (
    <article className={`hr-confirmation-card ${visualState}`}>
      <header><div><AlertTriangle aria-hidden="true" /><span>需要你的明确确认</span></div><time dateTime={block.expires_at}>有效至 {new Date(block.expires_at).toLocaleString("zh-CN")}</time></header>
      <h3>{cancellation ? "撤销申请确认" : "提交请假申请"}</h3>
      <dl>{Object.entries(block.preview).map(([key, value]) => <div key={key}><dt>{FIELD_LABELS[key] ?? key}</dt><dd>{formatValue(key, value)}</dd></div>)}</dl>
      {state === "expired" && <p className="hr-confirmation-state"><X aria-hidden="true" />确认已过期，请重新发起</p>}
      {state === "cancelled" && <p className="hr-confirmation-state"><RotateCcw aria-hidden="true" />已取消，本次没有提交申请</p>}
      {state === "failed" && <p className="hr-confirmation-state"><AlertTriangle aria-hidden="true" />确认失败，业务数据没有被标记为成功</p>}
      {state === "unrecoverable" && <p className="hr-confirmation-state"><AlertTriangle aria-hidden="true" />本次确认无法通过重复点击恢复，请返回修改并重新发起。</p>}
      <div className="hr-confirmation-actions">
        <button type="button" disabled={cancelUnavailable} onClick={onCancel}><RotateCcw aria-hidden="true" />返回修改</button>
        <button type="button" disabled={confirmUnavailable} onClick={onConfirm}><Check aria-hidden="true" />{state === "busy" ? "正在确认…" : state === "failed" ? "重试确认" : state === "unrecoverable" ? "无法重试" : cancellation ? "确认撤销" : "确认提交"}</button>
      </div>
    </article>
  );
}
