import { Download, ExternalLink, FileSearch, X } from "lucide-react";
import { useEffect, useState } from "react";
import { listDocumentChunks, type DocumentChunk, type PolicyDocument } from "../api/client";

export function DocumentDetailDrawer({ document, onClose }: { document: PolicyDocument; onClose(): void }) {
  const previewable = document.mime_type === "application/pdf"
    || document.mime_type === "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
  const [chunks, setChunks] = useState<DocumentChunk[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void listDocumentChunks(document.id).then(items => { if (active) setChunks(items); })
      .catch(() => { if (active) setError("暂时无法读取系统解析内容。"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [document.id]);
  return <div className="document-detail-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <aside className="document-detail" role="dialog" aria-modal="true" aria-label={document.display_name}>
      <header><div><span className="eyebrow">SOURCE INSPECTION</span><h2>{document.display_name}</h2><p>{document.chunk_count} 个当前活动片段</p></div><button aria-label="关闭文档详情" onClick={onClose}><X aria-hidden="true" /></button></header>
      <div className="document-file-actions">{previewable ? <a href={`/api/v1/documents/${document.id}/preview`} target="_blank" rel="noreferrer"><ExternalLink aria-hidden="true" />在线预览</a> : <span>此格式请查看系统解析内容</span>}<a href={`/api/v1/documents/${document.id}/content?download=true`}><Download aria-hidden="true" />下载原文件</a></div>
      <section className="parsed-content" aria-label="系统解析内容"><div className="parsed-heading"><FileSearch aria-hidden="true" /><div><h3>系统解析内容</h3><p>仅展示当前活动索引中的片段。</p></div></div>
        {loading && <p className="detail-state">正在读取解析内容…</p>}{error && <p className="admin-error" role="alert">{error}</p>}{!loading && !error && chunks.length === 0 && <p className="detail-state">当前没有可查看的活动片段。</p>}
        <ol>{chunks.map(chunk => <li key={chunk.id}><div><span>片段 {chunk.sequence + 1}</span><span>{chunk.page_number ? `第 ${chunk.page_number} 页` : chunk.location ?? "位置未标注"}</span></div>{chunk.heading_path && <strong>{chunk.heading_path}</strong>}<p>{chunk.text}</p></li>)}</ol>
      </section>
    </aside>
  </div>;
}
