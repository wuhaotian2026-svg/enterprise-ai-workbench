import { FileUp, Upload } from "lucide-react";
import { useState, type FormEvent } from "react";
import { ApiError, uploadDocument } from "../api/client";

const uploadErrors: Record<string, string> = {
  duplicate_document: "该文件内容已经上传，无需重复导入。",
  unsafe_filename: "文件名不安全，请移除路径符号或双重扩展名后重试。",
  empty_file: "文件内容为空，请选择包含制度内容的文件。",
  file_too_large: "文件超过系统允许的大小限制。",
  content_mismatch: "文件内容与扩展名不一致，请确认文件真实格式。",
};

export function UploadDialog({ onUploaded }: { onUploaded(): Promise<void> }) {
  const [file, setFile] = useState<File>(); const [uploading, setUploading] = useState(false); const [error, setError] = useState("");
  async function submit(event: FormEvent) { event.preventDefault(); if (!file) return; setUploading(true); setError(""); try { await uploadDocument(file); await onUploaded(); setFile(undefined); } catch (cause) { const code = cause instanceof ApiError ? cause.code : "upload_failed"; setError(uploadErrors[code] ?? "上传失败，请稍后重试；若问题持续，请联系系统管理员。"); } finally { setUploading(false); } }
  return <section className="upload-panel"><div><span className="eyebrow">INGEST / 01</span><h2>导入制度文件</h2><p>支持 PDF、DOCX、TXT，单个文件不超过系统限制。</p></div><form onSubmit={submit}><label className="file-picker" htmlFor="policy-file"><FileUp aria-hidden="true" /><span>{file?.name ?? "选择制度文件"}</span></label><input id="policy-file" className="visually-hidden" aria-label="选择制度文件" type="file" accept=".pdf,.docx,.txt" onChange={e => setFile(e.target.files?.[0])} disabled={uploading} /><button type="submit" disabled={!file || uploading}>{uploading ? "正在上传…" : "上传并建立索引"}<Upload aria-hidden="true" /></button>{error && <p role="alert">{error}</p>}</form></section>;
}
