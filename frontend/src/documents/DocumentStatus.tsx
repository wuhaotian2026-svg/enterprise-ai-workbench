import { AlertCircle, CheckCircle2, CircleDashed, PauseCircle } from "lucide-react";

const statusMap: Record<string, { label: string; tone: string; icon: typeof CheckCircle2 }> = {
  enabled: { label: "已启用", tone: "success", icon: CheckCircle2 },
  pending: { label: "等待处理", tone: "neutral", icon: CircleDashed },
  processing: { label: "处理中", tone: "neutral", icon: CircleDashed },
  disabled: { label: "已停用", tone: "muted", icon: PauseCircle },
  parse_failed: { label: "解析失败", tone: "error", icon: AlertCircle },
  index_failed: { label: "索引失败", tone: "error", icon: AlertCircle },
};
export function DocumentStatus({ status }: { status: string }) { const item = statusMap[status] ?? statusMap.pending; const Icon = item.icon; return <span className={`document-status ${item.tone}`}><Icon aria-hidden="true" />{item.label}</span>; }
