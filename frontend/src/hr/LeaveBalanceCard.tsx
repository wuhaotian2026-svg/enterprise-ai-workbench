import { CalendarRange } from "lucide-react";
import type { LeaveBalance } from "./types";

function days(value: string): string {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${parsed.toLocaleString("zh-CN")} 天` : `${value} 天`;
}

export function LeaveBalanceCard({ balance, queriedAt }: { balance: LeaveBalance; queriedAt: string }) {
  return (
    <article className="hr-balance-card">
      <header><div><CalendarRange aria-hidden="true" /><span>HR 业务数据</span></div><time dateTime={queriedAt}>查询于 {new Date(queriedAt).toLocaleString("zh-CN")}</time></header>
      <h3>{balance.leave_type_name}余额</h3>
      <strong className="hr-balance-available">{days(balance.available)}</strong>
      <dl>
        <div><dt>年度额度</dt><dd>{days(balance.entitled)}</dd></div>
        <div><dt>已使用</dt><dd>{days(balance.used)}</dd></div>
        <div><dt>审批占用</dt><dd>{days(balance.reserved)}</dd></div>
        <div><dt>余额年度</dt><dd>{balance.year}</dd></div>
      </dl>
    </article>
  );
}
