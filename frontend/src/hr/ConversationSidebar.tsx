import { MessageSquareText, Plus, Trash2 } from "lucide-react";
import type { HrConversationSummary } from "./types";

export function ConversationSidebar({
  conversations,
  selectedId,
  loading,
  deletingId,
  deleteError,
  onSelect,
  onDelete,
  onNew,
}: {
  conversations: HrConversationSummary[];
  selectedId?: string;
  loading: boolean;
  deletingId?: string;
  deleteError?: string;
  onSelect(id: string): void;
  onDelete(id: string): void;
  onNew(): void;
}) {
  return (
    <aside className="hr-conversation-sidebar" aria-label="HR 对话历史">
      <div className="hr-sidebar-heading">
        <span>CASE LOG / HR</span>
        <button type="button" onClick={onNew}><Plus aria-hidden="true" />新建办事</button>
      </div>
      <nav aria-label="历史办事会话">
        {loading && <p className="hr-sidebar-state">正在读取历史记录…</p>}
        {!loading && conversations.length === 0 && <p className="hr-sidebar-state">还没有历史办事。</p>}
        {conversations.map((item) => {
          const selected = selectedId === item.id;
          return (
            <div className={selected ? "hr-conversation-row active" : "hr-conversation-row"} key={item.id}>
              <button
                type="button"
                className="hr-conversation-item"
                aria-label={item.title}
                aria-current={selected ? "page" : undefined}
                onClick={() => onSelect(item.id)}
              >
                <MessageSquareText aria-hidden="true" />
                <span><strong>{item.title}</strong><small>{new Date(item.updated_at).toLocaleDateString("zh-CN")}</small></span>
              </button>
              {selected && (
                <button
                  type="button"
                  className="hr-conversation-delete"
                  aria-label={`删除 ${item.title}`}
                  disabled={deletingId === item.id}
                  onClick={() => onDelete(item.id)}
                >
                  <Trash2 aria-hidden="true" />
                  {deletingId === item.id ? "删除中…" : "删除"}
                </button>
              )}
            </div>
          );
        })}
      </nav>
      {deleteError && <p className="hr-conversation-delete-error" role="alert" aria-label="删除办事记录失败">{deleteError}</p>}
      <p className="hr-sidebar-boundary">业务事实来自 HR 系统<br />制度解释保留原文引用</p>
    </aside>
  );
}
