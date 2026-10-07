import { useState, type FormEvent } from "react";
import { ArrowRight, Building2, LockKeyhole, ShieldCheck } from "lucide-react";
import { ApiError } from "../api/client";
import { useAuth } from "./AuthProvider";

export function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault(); setSubmitting(true); setError("");
    try { await login(username, password); }
    catch (cause) {
      setError(cause instanceof ApiError && cause.code === "invalid_credentials"
        ? "用户名或密码不正确，请重新输入。" : "暂时无法登录，请稍后再试。");
    } finally { setSubmitting(false); }
  }
  return <main className="login-shell">
    <section className="login-manifest" aria-labelledby="manifest-title">
      <header className="brand-line"><Building2 aria-hidden="true" /><span>ENTERPRISE / AI WORKBENCH</span></header>
      <div className="manifest-copy">
        <p className="eyebrow">企业内部智能办公入口</p>
        <h1 id="manifest-title">让知识、流程与 AI，在同一个工作台协作。</h1>
        <p className="manifest-lead">统一连接可信制度知识、HR 办事、采购申请与人工审批。系统依据当前账号权限呈现能力，AI 不绕过权限，也不替代人工决策。</p>
      </div>
      <div className="workbench-modules" aria-label="工作台能力范围">
        {[
          ["01", "KNOWLEDGE"],
          ["02", "HR SERVICE"],
          ["03", "PROCUREMENT"],
          ["04", "APPROVAL"],
        ].map(([index, label]) => <span key={label}><small>{index}</small>{label}</span>)}
      </div>
      <ol className="trust-index">
        <li><span>01</span><div><strong>知识可信</strong><small>回答保留制度来源与适用边界</small></div></li>
        <li><span>02</span><div><strong>流程受控</strong><small>申请、确认与审批遵循权威状态</small></div></li>
        <li><span>03</span><div><strong>权限隔离</strong><small>只呈现角色与组织范围允许的能力</small></div></li>
      </ol>
    </section>
    <section className="login-panel" aria-labelledby="login-title">
      <div className="panel-axis" aria-hidden="true"><span>INTERNAL</span></div>
      <div className="login-form-wrap">
        <div className="security-note"><ShieldCheck aria-hidden="true" /><span>企业内部访问</span></div>
        <p className="section-number">ACCESS / 01</p>
        <h2 id="login-title">登录企业 AI 工作台</h2>
        <p className="login-intro">系统将根据账号权限进入对应的知识、办事、申请、审批或管理模块。</p>
        <form onSubmit={submit}>
          <label htmlFor="username">用户名</label>
          <input id="username" name="username" autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} disabled={submitting} required />
          <label htmlFor="password">密码</label>
          <div className="password-field"><LockKeyhole aria-hidden="true" /><input id="password" name="password" type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} disabled={submitting} required /></div>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button type="submit" disabled={submitting}>{submitting ? "正在验证…" : "登录"}<ArrowRight aria-hidden="true" /></button>
        </form>
        <p className="privacy-line">会话凭据仅通过 HttpOnly Cookie 保存。</p>
      </div>
    </section>
  </main>;
}
