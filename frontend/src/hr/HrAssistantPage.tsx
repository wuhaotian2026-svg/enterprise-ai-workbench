import { Bot, History, Send, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ApiError } from "../api/request";
import * as client from "./client";
import { ConversationSidebar } from "./ConversationSidebar";
import { TurnBlocks } from "./TurnBlocks";
import type { ConfirmationState } from "./ConfirmationCard";
import type { ExecutionResultBlock, HrConversationDetail, HrConversationSummary, HrStoredTurn } from "./types";
import "./hr.css";

function errorCode(cause: unknown): string | undefined {
  if (cause instanceof ApiError) return cause.code;
  if (typeof cause === "object" && cause !== null && "code" in cause && typeof cause.code === "string") return cause.code;
  return undefined;
}

function isConflict(cause: unknown): boolean {
  return cause instanceof ApiError ? cause.status === 409 : typeof cause === "object" && cause !== null && "status" in cause && cause.status === 409;
}

function isAmbiguousFailure(cause: unknown): boolean {
  if (!(cause instanceof ApiError)) {
    if (typeof cause !== "object" || cause === null || !("status" in cause)) return true;
    const status = cause.status;
    return typeof status !== "number" || status === 408 || status === 429 || status >= 500;
  }
  return cause.code === "request_failed"
    || cause.status === 408
    || cause.status === 429
    || cause.status >= 500;
}

export function HrAssistantPage() {
  const [conversations, setConversations] = useState<HrConversationSummary[]>([]);
  const [active, setActive] = useState<HrConversationDetail | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [liveMessage, setLiveMessage] = useState("");
  const [banner, setBanner] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [deletingId, setDeletingId] = useState<string>();
  const [deleteError, setDeleteError] = useState("");
  const [confirmationStates, setConfirmationStates] = useState<Record<string, ConfirmationState>>({});
  const operationIds = useRef(new Map<string, string>());
  const composer = useRef<HTMLTextAreaElement>(null);
  const conversationRequestVersion = useRef(0);
  const turnIntent = useRef<{ text: string; clientTurnId: string } | null>(null);
  const inFlightRef = useRef(false);

  const openConversation = useCallback(async (id: string) => {
    turnIntent.current = null;
    const requestVersion = ++conversationRequestVersion.current;
    setLoadingConversation(true);
    setBanner("");
    setDeleteError("");
    try {
      const detail = await client.getConversation(id);
      if (requestVersion === conversationRequestVersion.current) {
        setActive(detail);
        setHistoryOpen(false);
      }
    } catch {
      if (requestVersion === conversationRequestVersion.current) {
        setBanner("暂时无法读取这段办事记录。");
      }
    } finally {
      if (requestVersion === conversationRequestVersion.current) {
        setLoadingConversation(false);
      }
    }
  }, []);

  useEffect(() => {
    let mounted = true;
    void client.listConversations().then(async values => {
      if (!mounted) return;
      setConversations(values);
      if (values[0]) await openConversation(values[0].id);
    }).catch(() => mounted && setBanner("暂时无法读取 HR 对话历史。"))
      .finally(() => mounted && setLoadingHistory(false));
    return () => { mounted = false; };
  }, [openConversation]);

  async function createNew() {
    if (creating) return;
    turnIntent.current = null;
    setCreating(true);
    setBanner("");
    try {
      const created = await client.createConversation();
      conversationRequestVersion.current += 1;
      setLoadingConversation(false);
      setConversations(current => [created, ...current.filter(item => item.id !== created.id)]);
      setActive({ ...created, turns: [] });
      setHistoryOpen(false);
      setDraft("");
      window.setTimeout(() => composer.current?.focus(), 0);
    } catch {
      setBanner("暂时无法新建办事会话。");
    } finally {
      setCreating(false);
    }
  }

  async function ensureConversation(): Promise<HrConversationDetail> {
    if (active) return active;
    const created = await client.createConversation();
    const detail = { ...created, turns: [] };
    setConversations(current => [created, ...current]);
    setActive(detail);
    return detail;
  }

  async function submit(value = draft) {
    const text = value.trim();
    if (!text || inFlightRef.current) return;
    const intent = turnIntent.current?.text === text
      ? turnIntent.current
      : { text, clientTurnId: client.newClientId() };
    turnIntent.current = intent;
    inFlightRef.current = true;
    setSending(true);
    setLiveMessage("正在处理你的事项");
    setBanner("");
    try {
      const conversation = await ensureConversation();
      const result = await client.sendTurn(conversation.id, intent.text, intent.clientTurnId);
      const turn: HrStoredTurn = { ...result, text, created_at: new Date().toISOString() };
      setActive(current => current && current.id === conversation.id ? { ...current, turns: [...current.turns, turn], updated_at: turn.created_at } : current);
      setConversations(current => current.map(item => item.id === conversation.id ? { ...item, updated_at: turn.created_at } : item));
      turnIntent.current = null;
      setDraft("");
      setLiveMessage("处理完成");
    } catch (cause) {
      if (isConflict(cause) && active) {
        turnIntent.current = null;
        await openConversation(active.id);
        setBanner("状态已发生变化，已为你刷新当前对话。");
      } else {
        if (!isAmbiguousFailure(cause)) turnIntent.current = null;
        setBanner("本次请求未完成。内容已保留，可以安全重试。");
      }
      setLiveMessage("处理未完成");
    } finally {
      inFlightRef.current = false;
      setSending(false);
    }
  }

  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void submit();
  }

  function replaceConfirmation(id: string, replacement: ExecutionResultBlock) {
    setActive(current => current ? { ...current, turns: current.turns.map(turn => ({ ...turn, blocks: turn.blocks.map(block => block.type === "confirmation" && block.confirmation_id === id ? replacement : block) })) } : current);
  }

  async function refreshAfterConflict() {
    if (active) await openConversation(active.id);
    setBanner("状态已发生变化，已为你刷新当前对话。");
  }

  async function confirm(id: string) {
    if (confirmationStates[id] === "busy") return;
    const operationId = operationIds.current.get(id) ?? client.newClientId();
    operationIds.current.set(id, operationId);
    setConfirmationStates(current => ({ ...current, [id]: "busy" }));
    setLiveMessage("正在确认业务操作");
    try {
      const result = await client.confirmTool(id, operationId);
      replaceConfirmation(id, result);
      setLiveMessage("确认完成");
    } catch (cause) {
      const code = errorCode(cause);
      if (code === "confirmation_expired") {
        setConfirmationStates(current => ({ ...current, [id]: "expired" }));
        setBanner("确认已过期，请重新发起。");
      } else if (code === "tool_execution_non_retryable") {
        setConfirmationStates(current => ({ ...current, [id]: "unrecoverable" }));
      } else if (isConflict(cause)) {
        await refreshAfterConflict();
        setConfirmationStates(current => ({ ...current, [id]: "failed" }));
      } else {
        setConfirmationStates(current => ({ ...current, [id]: "failed" }));
        setBanner("确认未完成，系统没有将业务操作标记为成功。");
      }
      setLiveMessage("确认未完成");
    }
  }

  async function cancel(id: string) {
    if (confirmationStates[id] === "busy") return;
    setConfirmationStates(current => ({ ...current, [id]: "busy" }));
    try {
      await client.cancelTool(id);
      setConfirmationStates(current => ({ ...current, [id]: "cancelled" }));
      setLiveMessage("已取消确认");
    } catch (cause) {
      const code = errorCode(cause);
      setConfirmationStates(current => ({ ...current, [id]: code === "confirmation_expired" ? "expired" : "failed" }));
      setBanner(code === "confirmation_expired" ? "确认已过期，请重新发起。" : "暂时无法取消确认，请刷新后重试。");
    }
  }

  function useSuggestion(value: string) {
    setDraft(value);
    window.setTimeout(() => composer.current?.focus(), 0);
  }

  async function archiveConversation(id: string) {
    if (deletingId || inFlightRef.current) return;
    setDeletingId(id);
    setDeleteError("");
    try {
      await client.archiveConversation(id);
      const remaining = conversations.filter(item => item.id !== id);
      setConversations(remaining);
      turnIntent.current = null;
      if (active?.id === id) {
        conversationRequestVersion.current += 1;
        setActive(null);
        setLoadingConversation(false);
        if (remaining[0]) await openConversation(remaining[0].id);
      }
    } catch {
      setDeleteError("暂时无法删除这条办事记录，请稍后重试。");
    } finally {
      setDeletingId(undefined);
    }
  }

  return (
    <main className="hr-assistant-shell">
      <div className={historyOpen ? "hr-history-drawer open" : "hr-history-drawer"}>
        <ConversationSidebar conversations={conversations} selectedId={active?.id} loading={loadingHistory} deletingId={deletingId} deleteError={deleteError} onSelect={id => void openConversation(id)} onDelete={id => void archiveConversation(id)} onNew={() => void createNew()} />
      </div>
      <section className="hr-assistant-main">
        <header className="hr-assistant-header"><button type="button" className="hr-history-toggle" aria-label="打开请假办事记录" aria-expanded={historyOpen} onClick={() => setHistoryOpen(value => !value)}><History aria-hidden="true" /><span>办事记录</span></button><div className="hr-assistant-identity"><Bot aria-hidden="true" /><div><span>LEAVE SERVICE / CONTROLLED TOOLS</span><h1>AI 请假助手</h1><p>查询假期余额、了解请假制度、提交与撤销请假申请</p></div></div><p><ShieldCheck aria-hidden="true" />写操作必须由你确认</p></header>
        <div className="hr-assistant-feed" aria-busy={loadingConversation}>
          {banner && <p className="hr-assistant-banner" role="alert">{banner}</p>}
          {loadingConversation && <p className="hr-feed-state">正在读取办事记录…</p>}
          {!loadingConversation && (!active || active.turns.length === 0) && <section className="hr-assistant-intro"><span className="eyebrow">LEAVE OPERATIONS / 01</span><h2>把制度查询，<br />连接到实际办理。</h2><p>还没有办事记录</p><strong>请直接描述你要办理或查询的事项。</strong><div><span>可以尝试</span><button type="button" onClick={() => useSuggestion("查询我的年假余额")}>查询我的年假余额</button><button type="button" onClick={() => useSuggestion("我想申请年假")}>我想申请年假</button></div></section>}
          {active?.turns.map(turn => <article className="hr-turn" key={turn.client_turn_id}><div className="hr-user-request"><span>你提交的事项</span><p>{turn.text}</p></div><TurnBlocks blocks={turn.blocks} confirmationStates={confirmationStates} onSuggestion={useSuggestion} onConfirm={id => void confirm(id)} onCancel={id => void cancel(id)} onRetry={() => void submit(turn.text)} onClearDraft={() => void submit("清空草稿")} /></article>)}
        </div>
        <form className="hr-composer" onSubmit={event => { event.preventDefault(); void submit(); }}>
          <label htmlFor="hr-assistant-input">向 AI 请假助手说明事项</label>
          <div><textarea ref={composer} id="hr-assistant-input" rows={2} value={draft} disabled={sending} onChange={event => setDraft(event.target.value)} onKeyDown={onComposerKeyDown} placeholder="例如：我想在 8 月 20 日到 21 日请年假，用于家庭事务" /><button type="submit" aria-label="发送" disabled={sending || !draft.trim()}>{sending ? "处理中…" : "发送"}<Send aria-hidden="true" /></button></div>
          <p>Enter 发送 · Shift+Enter 换行 · 任何提交操作都会先展示确认卡</p>
        </form>
        <p className="visually-hidden" role="status" aria-live="polite">{liveMessage}</p>
      </section>
      {historyOpen && <button type="button" className="hr-history-scrim" aria-label="关闭 HR 对话历史" onClick={() => setHistoryOpen(false)} />}
    </main>
  );
}
