import { AlertCircle, CheckCircle2, Clock3, FileText, ListChecks, NotebookPen, RotateCcw, Trash2 } from "lucide-react";
import { ConfirmationCard, type ConfirmationState } from "./ConfirmationCard";
import { LeaveBalanceCard } from "./LeaveBalanceCard";
import type { ConfirmationTerminalBlock, ExecutionResultBlock, HrTurnBlock, LeaveBalance, LeaveRequest } from "./types";

const MISSING_LABELS: Record<string, string> = {
  leave_type_code: "假期类型",
  start_date: "开始日期",
  end_date: "结束日期",
  reason: "请假原因",
  request_id: "申请编号",
  year: "请假年份",
  date_range: "请假日期",
};

const INTERNAL_SUGGESTION = /^[a-z][a-z0-9_.\[\]-]*:[a-z][a-z0-9_]*$/i;

const ERROR_MESSAGES: Record<string, string> = {
  tool_provider_timeout: "HR 智能服务响应超时，未执行任何写操作。",
  tool_provider_rate_limited: "HR 智能服务当前繁忙，未执行任何写操作。",
  tool_provider_unavailable: "HR 智能服务暂时不可用，未执行任何写操作。",
  leave_balance_insufficient: "可用余额不足，系统没有生成可执行确认。",
  leave_request_overlap: "所选日期与已有申请重叠，系统没有生成可执行确认。",
  work_calendar_incomplete: "企业工作日历不完整，暂时不能计算本次申请。",
  invalid_tool_arguments: "提供的信息无法通过业务校验，请补充或修改后重试。",
  tool_write_limit_exceeded: "一次对话只能提出一项写操作，请拆分办理。",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isLeaveBalance(value: unknown): value is LeaveBalance {
  return isRecord(value)
    && typeof value.leave_type_name === "string"
    && typeof value.available === "string"
    && typeof value.entitled === "string"
    && typeof value.used === "string"
    && typeof value.reserved === "string"
    && typeof value.year === "number";
}

function isLeaveRequest(value: Record<string, unknown>): value is Record<string, unknown> & LeaveRequest {
  return typeof value.request_number === "string" && typeof value.status === "string";
}

function statusCopy(status: unknown): string {
  return status === "pending" ? "已提交，等待 HR 审核" : status === "approved" ? "申请已批准" : status === "rejected" ? "申请已驳回" : status === "cancelled" ? "申请已撤销" : "业务操作已完成";
}

function ExecutionResult({ block }: { block: ExecutionResultBlock }) {
  const request = block.result;
  return (
    <article className="hr-execution-card">
      <header><CheckCircle2 aria-hidden="true" /><span>HR 系统执行结果</span></header>
      <h3>{statusCopy(request.status)}</h3>
      {isLeaveRequest(request) && <><strong>{request.request_number}</strong><dl>
        {typeof request.leave_type_name === "string" && <div><dt>假期类型</dt><dd>{request.leave_type_name}</dd></div>}
        {typeof request.start_date === "string" && <div><dt>日期</dt><dd>{request.start_date} — {String(request.end_date ?? "")}</dd></div>}
        {typeof request.workday_count === "string" && <div><dt>工作日数</dt><dd>{request.workday_count} 天</dd></div>}
        <div><dt>当前状态</dt><dd>{request.status}</dd></div>
      </dl></>}
    </article>
  );
}

function ConfirmationTerminal({ block }: { block: ConfirmationTerminalBlock }) {
  const cancelled = block.status === "cancelled";
  const expired = block.status === "expired";
  const Icon = cancelled ? RotateCcw : expired ? Clock3 : CheckCircle2;
  return (
    <article className={`hr-confirmation-terminal ${block.status}`} role="status">
      <header><Icon aria-hidden="true" /><span>请假操作确认</span></header>
      <h3>{cancelled ? "确认已取消" : expired ? "确认已过期" : "操作已经完成"}</h3>
      <p>{cancelled
        ? "已取消，本次没有提交申请"
        : expired
          ? "确认已过期，请重新发起"
          : "该操作已经执行，不能再次确认"}</p>
    </article>
  );
}

function GenericFacts({ label, value, queriedAt }: { label: string; value: unknown; queriedAt: string }) {
  const rows = Array.isArray(value) ? value : [value];
  return (
    <article className="hr-facts-card">
      <header><ListChecks aria-hidden="true" /><span>HR 业务数据</span><time dateTime={queriedAt}>{new Date(queriedAt).toLocaleString("zh-CN")}</time></header>
      <h3>{label === "leave_duration" ? "工作日计算" : label === "leave_requests" ? "请假申请" : label === "leave_request" ? "申请详情" : "业务查询结果"}</h3>
      {rows.map((row, index) => <dl key={index}>{isRecord(row) ? Object.entries(row).map(([key, item]) => <div key={key}><dt>{key}</dt><dd>{typeof item === "object" ? JSON.stringify(item) : String(item ?? "—")}</dd></div>) : <div><dt>结果</dt><dd>{String(row ?? "—")}</dd></div>}</dl>)}
    </article>
  );
}

export function TurnBlocks({
  blocks,
  confirmationStates,
  onSuggestion,
  onConfirm,
  onCancel,
  onRetry,
  onClearDraft,
}: {
  blocks: HrTurnBlock[];
  confirmationStates: Record<string, ConfirmationState>;
  onSuggestion(value: string): void;
  onConfirm(id: string): void;
  onCancel(id: string): void;
  onRetry(): void;
  onClearDraft(): void;
}) {
  return <div className="hr-turn-blocks">{blocks.map((block, index) => {
    switch (block.type) {
      case "text": return <p className="hr-answer-text" key={index}>{block.text}</p>;
      case "policy_citations": return <section className="hr-policy-card" key={index} aria-label="制度依据"><header><FileText aria-hidden="true" /><span>制度依据</span></header>{block.citations.map(citation => <details key={citation.chunk_id}><summary>{citation.document_name}{citation.page_number ? ` · 第 ${citation.page_number} 页` : ""}</summary><blockquote>{citation.evidence_snapshot}</blockquote></details>)}</section>;
      case "business_facts": return <div className="hr-business-facts" key={index}>{block.facts.flatMap((fact, factIndex) => Array.isArray(fact.value) && fact.value.every(isLeaveBalance) ? fact.value.map(balance => <LeaveBalanceCard key={`${factIndex}-${balance.leave_type_code}`} balance={balance} queriedAt={block.queried_at} />) : [<GenericFacts key={factIndex} label={fact.label} value={fact.value} queriedAt={block.queried_at} />])}</div>;
      case "clarification": {
        const suggestions = block.suggestions.filter(
          item => !INTERNAL_SUGGESTION.test(item),
        );
        return <section className="hr-clarification-card" key={index}><span>需要补充信息</span><h3>{block.missing_fields.map(item => MISSING_LABELS[item] ?? "待补充信息").join("、")}</h3><div>{suggestions.map(item => <button type="button" key={item} onClick={() => onSuggestion(item)}>{item}</button>)}</div></section>;
      }
      case "assistant_draft": return <section className="hr-draft-card" key={index} aria-label="当前 HR 办事草稿"><header><NotebookPen aria-hidden="true" /><div><span>已自动保留本会话中你明确提供的信息</span><strong>当前请假草稿</strong></div></header><dl>{Object.entries(block.fields).map(([name, value]) => <div key={name}><dt>{MISSING_LABELS[name] ?? "已记录信息"}</dt><dd>{name === "leave_type_code" ? value === "annual" ? "年假" : value === "compensatory" ? "调休" : String(value) : String(value)}</dd></div>)}</dl>{block.pending_fields.length > 0 && <p>待确认：{block.pending_fields.map(item => MISSING_LABELS[item] ?? "待确认信息").join("、")}</p>}{block.missing_fields.length > 0 && <p>还需补充：{block.missing_fields.map(item => MISSING_LABELS[item] ?? "待补充信息").join("、")}</p>}<button type="button" onClick={onClearDraft}><Trash2 aria-hidden="true" />清空草稿</button></section>;
      case "confirmation": {
        const computed = new Date(block.expires_at).getTime() <= Date.now() ? "expired" : "active";
        return <ConfirmationCard key={block.confirmation_id} block={block} state={confirmationStates[block.confirmation_id] ?? computed} onConfirm={() => onConfirm(block.confirmation_id)} onCancel={() => onCancel(block.confirmation_id)} />;
      }
      case "confirmation_terminal": return <ConfirmationTerminal key={block.confirmation_id} block={block} />;
      case "execution_result": return <ExecutionResult key={index} block={block} />;
      case "error": return <article className="hr-error-card" key={index}><AlertCircle aria-hidden="true" /><div><strong>{ERROR_MESSAGES[block.code] ?? "本次处理未完成，且没有执行未经确认的写操作。"}</strong><code>{block.code}</code>{block.retryable && <button type="button" onClick={onRetry}>重试本次请求</button>}</div></article>;
      default: return assertNever(block);
    }
  })}</div>;
}

function assertNever(value: never): never {
  throw new Error(`Unsupported HR turn block: ${JSON.stringify(value)}`);
}
