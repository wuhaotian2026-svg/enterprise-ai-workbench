import { Clock3, Plus } from "lucide-react";
import type { QuestionSummary } from "../api/client";

export function HistorySidebar({ items, selectedId, deletingId, deleteError, onSelect, onDelete, onNew }: {
  items: QuestionSummary[];
  selectedId?: string;
  deletingId?: string;
  deleteError?: string;
  onSelect(id: string): void;
  onDelete(id: string): void;
  onNew(): void;
}) {
  return <aside className="history-sidebar" aria-label="提问历史">
    <div className="history-heading"><span>ARCHIVE / 01</span><button onClick={onNew}><Plus aria-hidden="true" />新问题</button></div>
    <nav>{items.length === 0 ? <p className="history-empty">还没有历史问题。<br />从右侧开始第一次查询。</p> : items.map(item => {
      const selected = item.id === selectedId;
      return <div key={item.id} className={selected ? "history-row active" : "history-row"}>
        <button className="history-item" onClick={() => onSelect(item.id)}>
          <Clock3 aria-hidden="true" /><span><strong>{item.text}</strong><small>{new Date(item.created_at).toLocaleDateString("zh-CN")}</small></span>
        </button>
        {selected && <button type="button" className="history-delete" onClick={() => onDelete(item.id)} disabled={deletingId === item.id}>删除</button>}
      </div>;
    })}</nav>
    {deleteError && <p className="history-delete-error" role="alert" aria-label="删除历史记录失败">{deleteError}</p>}
  </aside>;
}
