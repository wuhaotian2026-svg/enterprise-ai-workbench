import { ChevronDown, FileText } from "lucide-react";
import { useState } from "react";
import type { Citation } from "../api/client";

export function CitationList({ citations }: { citations: Citation[] }) {
  const [open, setOpen] = useState<number[]>([]);
  return <section className="citation-section" aria-labelledby="citation-title"><div className="citation-heading"><span id="citation-title">EVIDENCE / 引用证据</span><small>{citations.length} 条</small></div>
    <ol>{citations.map(c => { const expanded = open.includes(c.number); return <li key={`${c.chunk_id}-${c.number}`}>
      <button aria-expanded={expanded} onClick={() => setOpen(x => expanded ? x.filter(n => n !== c.number) : [...x, c.number])}>
        <span><FileText aria-hidden="true" />查看引用 {c.number}</span><ChevronDown aria-hidden="true" />
      </button>
      {expanded && <div className="citation-copy">{c.source_status === "disabled" && <p className="source-warning">来源已停用；以下内容是回答生成时保存的证据快照。</p>}<div className="citation-source"><strong>{c.document_name}</strong><span>{[c.page_number ? `第 ${c.page_number} 页` : null, c.heading_path || c.location].filter(Boolean).join(" · ") || "原文位置未标注"}</span></div><blockquote>{c.evidence_snapshot}</blockquote><small>证据快照 · {c.chunk_id}</small></div>}
    </li>})}</ol>
  </section>;
}
