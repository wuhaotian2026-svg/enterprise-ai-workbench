import { ApiError, request } from "./request";

export { ApiError } from "./request";
export type CurrentUser = { username: string; role: "employee" | "hr" | "admin" };

export async function getCurrentUser(): Promise<CurrentUser | null> {
  try {
    return await (await request("/auth/me")).json();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export async function login(username: string, password: string): Promise<void> {
  await request("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
}

export async function logout(): Promise<void> {
  await request("/auth/logout", { method: "POST" });
}

export type QuestionSummary = { id: string; text: string; status: string; created_at: string };
export type Citation = { number: number; chunk_id: string; evidence_snapshot: string; source_status?: "enabled" | "disabled"; document_name: string; mime_type: string; page_number: number | null; heading_path: string | null; location: string | null };
type BaseAnswer = {
  id: string;
  evidence_score: number | null;
  citations: Citation[];
};
export type Answer =
  | (BaseAnswer & { status: "answered"; text: string; refusal_reason: null; clarification: null })
  | (BaseAnswer & { status: "needs_clarification"; text: string; refusal_reason: null; clarification: { questions: string[] } })
  | (BaseAnswer & { status: "abstained"; text: null; refusal_reason: string; clarification: null })
  | (BaseAnswer & { status: "failed"; text: null; refusal_reason: string | null; clarification: null });
export type QuestionDetail = QuestionSummary & { answer: Answer | null };
export type Feedback = { id: string; is_helpful: boolean; reason: string | null; updated_at: string };

export async function listQuestions(): Promise<QuestionSummary[]> { return await (await request("/questions")).json(); }
export async function getQuestion(id: string): Promise<QuestionDetail> { return await (await request(`/questions/${id}`)).json(); }
export async function deleteQuestion(id: string): Promise<void> { await request(`/questions/${encodeURIComponent(id)}`, { method: "DELETE" }); }
export async function askQuestion(text: string, questionId?: string): Promise<QuestionDetail> {
  return await (await request("/questions", { method: "POST", body: JSON.stringify({ text, ...(questionId ? { question_id: questionId } : {}) }) })).json();
}
export async function submitFeedback(answerId: string, isHelpful: boolean, reason?: string): Promise<Feedback> {
  return await (await request(`/answers/${answerId}/feedback`, { method: "POST", body: JSON.stringify({ is_helpful: isHelpful, reason }) })).json();
}

export type PolicyDocument = { id: string; display_name: string; mime_type: string; status: string; is_enabled: boolean; error_code: string | null; updated_at: string; chunk_count: number };
export type DocumentChunk = { id: string; sequence: number; heading_path: string | null; page_number: number | null; location: string | null; text: string };
export async function listDocuments(): Promise<PolicyDocument[]> { return await (await request("/documents")).json(); }
export async function listDocumentChunks(id: string): Promise<DocumentChunk[]> { return await (await request(`/documents/${encodeURIComponent(id)}/chunks`)).json(); }
export async function uploadDocument(file: File): Promise<PolicyDocument> {
  const data = new FormData(); data.append("file", file);
  const csrf = document.cookie.split("; ").find(item => item.startsWith("policy_csrf="))?.split("=")[1];
  const response = await fetch("/api/v1/documents", { method: "POST", credentials: "include", headers: csrf ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}, body: data });
  if (!response.ok) { const body = await response.json().catch(() => ({ code: "upload_failed" })); throw new ApiError(response.status, body.code ?? "upload_failed", body); }
  return await response.json();
}
async function documentAction(id: string, action: "disable" | "enable" | "reindex"): Promise<void> { await request(`/documents/${id}/${action}`, { method: "POST" }); }
export async function disableDocument(id: string): Promise<void> { await documentAction(id, "disable"); }
export async function enableDocument(id: string): Promise<void> { await documentAction(id, "enable"); }
export async function reindexDocument(id: string): Promise<void> { await documentAction(id, "reindex"); }
