import {
  Building2,
  LogOut,
  Menu,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { CurrentUser } from "../api/client";
import { ApiError } from "../api/request";
import { listWorkbenchModules, recordModuleOpened } from "../workbench/client";
import {
  isRegisteredModuleKey,
  moduleRegistry,
  type RegisteredModule,
} from "../workbench/moduleRegistry";
import type {
  AllowedModule,
  WorkbenchModuleKey,
} from "../workbench/types";

type VisibleModule = RegisteredModule & AllowedModule;
type LoadState = "loading" | "ready" | "error";

export function AppShell({ user, onLogout }: { user: CurrentUser; onLogout(): Promise<void> | void }) {
  const [allowed, setAllowed] = useState<VisibleModule[]>([]);
  const [active, setActive] = useState<WorkbenchModuleKey | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [reloadToken, setReloadToken] = useState(0);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const reportedTransition = useRef<string | null>(null);

  useEffect(() => {
    let disposed = false;
    setLoadState("loading");
    setAllowed([]);
    void listWorkbenchModules()
      .then((response) => {
        if (disposed) return;
        const registered = response.modules.flatMap<VisibleModule>((item) => {
          if (!isRegisteredModuleKey(item.key)) return [];
          const local = moduleRegistry[item.key];
          return [{ ...local, ...item, key: local.key }];
        });
        setAllowed(registered);
        setActive((current) => (
          registered.some((item) => item.key === current)
            ? current
            : registered.find((item) => item.key === "knowledge")?.key
              ?? registered[0]?.key
              ?? null
        ));
        setLoadState("ready");
      })
      .catch(() => {
        if (disposed) return;
        setLoadState("error");
      });
    return () => {
      disposed = true;
    };
  }, [reloadToken, user.role, user.username]);

  useEffect(() => {
    if (loadState !== "ready" || active === null) return;
    const transition = `${user.username}:${user.role}:${active}`;
    if (reportedTransition.current === transition) return;
    reportedTransition.current = transition;
    void recordModuleOpened(active).catch((error: unknown) => {
      const code = error instanceof ApiError ? error.code : "request_failed";
      console.warn("workbench_module_event_failed", code);
    });
  }, [active, loadState, user.role, user.username]);

  function selectModule(key: WorkbenchModuleKey) {
    if (!allowed.some((item) => item.key === key)) return;
    setActive(key);
    setDrawerOpen(false);
  }

  const activeModule = allowed.find((item) => item.key === active);

  return (
    <div className={drawerOpen ? "workbench-shell drawer-open" : "workbench-shell"}>
      <aside className="workbench-rail" id="workbench-navigation" aria-label="工作台模块">
        <div className="workbench-mark"><Building2 aria-hidden="true" /><span>ENTERPRISE / AI WORKBENCH</span></div>
        <nav>
          {allowed.map(({ key, label, index, icon: Icon }) => (
            <button key={key} type="button" className={active === key ? "active" : ""} aria-label={label} aria-current={active === key ? "page" : undefined} onClick={() => selectModule(key)}>
              <span>{index}</span><Icon aria-hidden="true" /><strong>{label}</strong>
            </button>
          ))}
        </nav>
        <p>可信知识 · 受控工具 · 人工决策</p>
      </aside>
      <header className="workbench-topbar">
        <button className="workbench-menu" type="button" aria-label={drawerOpen ? "关闭工作台导航" : "打开工作台导航"} aria-expanded={drawerOpen} aria-controls="workbench-navigation" onClick={() => setDrawerOpen((value) => !value)}>
          {drawerOpen ? <X aria-hidden="true" /> : <Menu aria-hidden="true" />}
        </button>
        <div><span>{user.role === "hr" ? "HR REVIEWER" : user.role.toUpperCase()}</span><strong>{user.username}</strong></div>
        <button className="workbench-logout" type="button" onClick={() => void onLogout()}><LogOut aria-hidden="true" />退出</button>
      </header>
      <section className="workbench-stage" aria-label="当前工作模块">
        {loadState === "loading" && (
          <div className="module-pending" role="status">正在加载工作台模块…</div>
        )}
        {loadState === "error" && (
          <div className="module-pending">
            <p role="alert">工作台模块加载失败</p>
            <button type="button" onClick={() => setReloadToken((value) => value + 1)}>重试</button>
          </div>
        )}
        {loadState === "ready" && allowed.length === 0 && (
          <div className="module-pending">当前账号没有可用的工作台模块</div>
        )}
        {loadState === "ready" && activeModule?.render({ user, onLogout })}
      </section>
      {drawerOpen && <button type="button" className="workbench-scrim" aria-label="关闭工作台导航" onClick={() => setDrawerOpen(false)} />}
    </div>
  );
}
