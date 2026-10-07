import { Eye, LogOut, Play, RefreshCw, ShieldCheck, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ApiError, disableDocument, enableDocument, listDocuments, reindexDocument, type PolicyDocument } from "../api/client";
import { DocumentDetailDrawer } from "./DocumentDetailDrawer";
import { DocumentStatus } from "./DocumentStatus";
import { UploadDialog } from "./UploadDialog";

export function DocumentAdminPage({ username, onLogout, embedded = false }: { username: string; onLogout(): Promise<void> | void; embedded?: boolean }) {
  const [documents, setDocuments] = useState<PolicyDocument[]>([]);
  const [confirming, setConfirming] = useState<PolicyDocument>();
  const [inspecting, setInspecting] = useState<PolicyDocument>();
  const [error, setError] = useState("");
  const refresh = useCallback(async () => { setDocuments(await listDocuments()); }, []);
  useEffect(() => { void refresh().catch(() => setError("暂时无法读取文档列表。")); }, [refresh]);
  const ingestionActive = documents.some(document => ["pending", "processing"].includes(document.status));
  useEffect(() => {
    if (!ingestionActive) return;
    const timer = window.setInterval(() => {
      void refresh().catch(() => setError("暂时无法更新文档状态。"));
    }, 2000);
    return () => window.clearInterval(timer);
  }, [ingestionActive, refresh]);
  async function action(operation: () => Promise<void>) { setError(""); try { await operation(); await refresh(); } catch (cause) { const requestId = cause instanceof ApiError ? String(cause.details.request_id ?? "无") : "无"; const code = cause instanceof ApiError ? cause.code : "operation_failed"; setError(`操作失败：${code}；请求 ID：${requestId}`); } }
  return <main className="admin-shell">
    {!embedded && <header className="admin-header"><div><ShieldCheck aria-hidden="true" /><span>POLICY / ADMINISTRATION</span></div><div className="user-block"><span>{username}</span><button onClick={() => void onLogout()}><LogOut aria-hidden="true" />退出</button></div></header>}
    <div className="admin-content"><section className="admin-title"><span className="eyebrow">KNOWLEDGE OPERATIONS / 02</span><h1>制度资料管理</h1><p>维护员工问答所依据的有效制度。停用不会删除源文件或历史证据。</p></section><UploadDialog onUploaded={refresh} />{error && <p className="admin-error" role="alert">{error}</p>}
      <section className="document-register"><div className="register-heading"><h2>文档登记册</h2><span>{documents.length} 份</span></div><div className="document-table" role="table" aria-label="制度文档"><div className="document-row header" role="row"><span>文件</span><span>类型</span><span>状态</span><span>更新 / 片段</span><span>错误摘要</span><span>操作</span></div>{documents.map(doc => <div className="document-row" role="row" key={doc.id}><strong>{doc.display_name}</strong><span>{doc.mime_type.includes("pdf") ? "PDF" : doc.mime_type.includes("word") ? "DOCX" : "TXT"}</span><DocumentStatus status={doc.status} /><span>{new Date(doc.updated_at).toLocaleDateString("zh-CN")} · {doc.chunk_count}</span><code>{doc.error_code ?? "—"}</code><div className="document-actions"><button aria-label={`查看 ${doc.display_name}`} onClick={() => setInspecting(doc)}><Eye aria-hidden="true" />查看</button>{doc.is_enabled && <button aria-label={`停用 ${doc.display_name}`} onClick={() => setConfirming(doc)}>停用</button>}{doc.status === "disabled" && <button aria-label={`重新启用 ${doc.display_name}`} onClick={() => void action(() => enableDocument(doc.id))}><Play aria-hidden="true" />重新启用</button>}{["parse_failed", "index_failed", "disabled"].includes(doc.status) && <button aria-label={`重新索引 ${doc.display_name}`} onClick={() => void action(() => reindexDocument(doc.id))}><RefreshCw aria-hidden="true" />重新索引</button>}</div></div>)}</div></section>
    </div>
    {inspecting && <DocumentDetailDrawer document={inspecting} onClose={() => setInspecting(undefined)} />}
    {confirming && <div className="confirm-overlay" role="dialog" aria-modal="true" aria-labelledby="confirm-title"><section><button className="dialog-close" aria-label="关闭" onClick={() => setConfirming(undefined)}><X aria-hidden="true" /></button><span className="eyebrow">CONFIRM CHANGE</span><h2 id="confirm-title">停用这份制度？</h2><p>停用后，新问题将不会检索“{confirming.display_name}”。这不会删除源文件或历史答案，管理员之后仍可重新启用或重新索引。</p><div><button onClick={() => setConfirming(undefined)}>取消</button><button onClick={() => { const item = confirming; setConfirming(undefined); void action(() => disableDocument(item.id)); }}>确认停用</button></div></section></div>}
  </main>;
}
