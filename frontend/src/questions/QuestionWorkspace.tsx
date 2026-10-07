import { BookOpenText, History, LogOut, Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError, askQuestion, deleteQuestion, getQuestion, listQuestions, submitFeedback, type QuestionDetail, type QuestionSummary } from "../api/client";
import { AnswerDocument } from "./AnswerDocument";
import { HistorySidebar } from "./HistorySidebar";

type PendingQuestionIntent = { text: string; questionId: string };

function isAmbiguousFailure(cause: unknown): boolean {
  if (!(cause instanceof ApiError)) return true;
  return cause.code === "request_failed"
    || cause.status === 408
    || cause.status === 429
    || cause.status >= 500;
}

export function QuestionWorkspace({ username, onLogout, embedded = false }: { username: string; onLogout(): Promise<void> | void; embedded?: boolean }) {
  const [history, setHistory] = useState<QuestionSummary[]>([]); const [selected, setSelected] = useState<QuestionDetail | null>(null);
  const [text, setText] = useState(""); const [sending, setSending] = useState(false); const [error, setError] = useState("");
  const [retryId, setRetryId] = useState<string>(); const [corpusUnavailable, setCorpusUnavailable] = useState(false); const [drawer, setDrawer] = useState(false);
  const [deletingId, setDeletingId] = useState<string>(); const [deleteError, setDeleteError] = useState("");
  const pendingIntent = useRef<PendingQuestionIntent | null>(null);
  const inFlightRef = useRef(false);
  useEffect(() => { void listQuestions().then(setHistory).catch(() => setError("暂时无法读取历史记录。")); }, []);
  async function open(id: string) { setDeleteError(""); setSelected(await getQuestion(id)); setDrawer(false); }
  async function send() {
    const question = text.trim();
    if (!question || inFlightRef.current) return;
    const intent = pendingIntent.current?.text === question
      ? pendingIntent.current
      : { text: question, questionId: crypto.randomUUID() };
    pendingIntent.current = intent;
    inFlightRef.current = true;
    setSending(true);
    setError("");
    try {
      const result = await askQuestion(intent.text, intent.questionId);
      pendingIntent.current = null;
      setSelected(result);
      setRetryId(undefined);
      setText("");
      setCorpusUnavailable(result.answer?.refusal_reason === "corpus_unavailable");
      setHistory(await listQuestions());
    } catch (cause) {
      if (isAmbiguousFailure(cause)) {
        setRetryId(intent.questionId);
        setError(cause instanceof ApiError && cause.code === "model_timeout"
          ? "模型暂时没有响应，问题已保留，可以安全重试。"
          : "本次请求未完成。问题已保留，可以安全重试。");
      } else {
        pendingIntent.current = null;
        setRetryId(undefined);
        setError("暂时无法完成查询，请稍后再试。");
      }
    } finally {
      inFlightRef.current = false;
      setSending(false);
    }
  }
  function reset() { pendingIntent.current = null; setSelected(null); setText(""); setError(""); setDeleteError(""); setRetryId(undefined); setDrawer(false); }
  async function remove(id: string) {
    setDeletingId(id); setDeleteError("");
    try {
      await deleteQuestion(id);
      setHistory(current => current.filter(item => item.id !== id));
      if (selected?.id === id) reset();
    } catch {
      setDeleteError("暂时无法删除这条历史记录，请稍后重试。");
    } finally {
      setDeletingId(undefined);
    }
  }
  return <main className="question-shell">
    <div className={drawer ? "history-drawer open" : "history-drawer"}><HistorySidebar items={history} selectedId={selected?.id} deletingId={deletingId} deleteError={deleteError} onSelect={id => void open(id)} onDelete={id => void remove(id)} onNew={reset} /></div>
    <section className="question-main"><header className="workspace-header"><button className="mobile-history" aria-label="打开提问历史" onClick={() => setDrawer(!drawer)}><History aria-hidden="true" /><span>提问历史</span></button>{!embedded && <><div className="workspace-brand"><BookOpenText aria-hidden="true" /><span>POLICY / KNOWLEDGE DESK</span></div><div className="user-block"><span>{username}</span><button onClick={() => void onLogout()}><LogOut aria-hidden="true" />退出</button></div></>}</header>
      <div className="workspace-content">{selected ? <AnswerDocument question={selected} onFeedback={(id, helpful) => submitFeedback(id, helpful).then(() => undefined)} /> : <section className="question-intro"><span className="eyebrow">ASK / 02</span><h1>从制度原文开始，<br />得到可追溯的回答。</h1><p>只根据已启用的公司制度回答。证据不足或冲突时，系统会明确拒答。</p></section>}</div>
      <form className="question-composer" onSubmit={e => { e.preventDefault(); void send(); }}><label htmlFor="policy-question">向制度知识库提问</label><div><textarea id="policy-question" value={text} onChange={e => setText(e.target.value)} onKeyDown={event => {
        if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
        event.preventDefault();
        if (!sending && !corpusUnavailable && text.trim()) void send();
      }} disabled={sending || corpusUnavailable} placeholder={corpusUnavailable ? "等待管理员启用制度资料" : "例如：一线城市出差住宿标准是多少？"} rows={2} /><button type="submit" disabled={sending || corpusUnavailable || !text.trim()}>{sending ? "正在检索…" : "发送问题"}<Send aria-hidden="true" /></button></div><p className="composer-shortcut">Enter 发送 · Shift+Enter 换行</p>{error && <p className="workspace-error" role="alert">{error}{retryId && <button type="button" onClick={() => void send()}>重试问题</button>}</p>}</form>
    </section>
  </main>;
}
