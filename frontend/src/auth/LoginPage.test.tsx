import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../app/App";
import * as client from "../api/client";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, getCurrentUser: vi.fn(), login: vi.fn(), logout: vi.fn(), listQuestions: vi.fn().mockResolvedValue([]) };
});
vi.mock("../workbench/client", () => ({
  recordModuleOpened: vi.fn().mockResolvedValue(undefined),
  listWorkbenchModules: vi.fn().mockResolvedValue({
    catalog_version: "2026-08-16",
    modules: [
      { key: "knowledge", label: "制度知识库", index: "01" },
      { key: "hr-assistant", label: "HR 办事助手", index: "02" },
      { key: "my-requests", label: "我的申请", index: "03" },
    ],
  }),
}));

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("authentication shell", () => {
  it("falls back to the login page when session restoration is unavailable", async () => {
    vi.mocked(client.getCurrentUser).mockRejectedValue(new Error("backend unavailable"));
    render(<App />);
    expect(await screen.findByRole("heading", { name: "登录企业 AI 工作台" })).toBeInTheDocument();
  });

  it("restores a missing session and shows a labelled login form", async () => {
    vi.mocked(client.getCurrentUser).mockResolvedValue(null);
    render(<App />);
    expect(await screen.findByRole("heading", { name: "登录企业 AI 工作台" })).toBeInTheDocument();
    expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toHaveAttribute("type", "password");
  });

  it("presents the unified enterprise AI workbench before authentication", async () => {
    vi.mocked(client.getCurrentUser).mockResolvedValue(null);
    render(<App />);

    expect(await screen.findByText("ENTERPRISE / AI WORKBENCH")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "让知识、流程与 AI，在同一个工作台协作。" })).toBeInTheDocument();
    expect(screen.getByText("知识可信")).toBeInTheDocument();
    expect(screen.getByText("流程受控")).toBeInTheDocument();
    expect(screen.getByText("权限隔离")).toBeInTheDocument();
    for (const label of ["KNOWLEDGE", "HR SERVICE", "PROCUREMENT", "APPROVAL"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.queryByText("登录制度知识库")).not.toBeInTheDocument();
  });

  it("submits by keyboard, disables controls while loading, and enters the workspace", async () => {
    let resolveLogin!: () => void;
    const deferred = new Promise<void>((resolve) => { resolveLogin = resolve; });
    vi.mocked(client.getCurrentUser).mockResolvedValue(null);
    vi.mocked(client.login).mockReturnValue(deferred);
    render(<App />); const user = userEvent.setup();
    await screen.findByLabelText("用户名");
    await user.type(screen.getByLabelText("用户名"), "employee");
    await user.type(screen.getByLabelText("密码"), "secret");
    const keyboardSubmit = user.keyboard("{Enter}");
    expect(await screen.findByRole("button", { name: "正在验证…" })).toBeDisabled();
    resolveLogin();
    await keyboardSubmit;
    await waitFor(() => expect(screen.getByRole("heading", { name: /从制度原文开始/ })).toBeInTheDocument());
  });

  it("shows one safe error message and supports logout", async () => {
    vi.mocked(client.getCurrentUser).mockResolvedValue(null);
    vi.mocked(client.login).mockRejectedValue(new client.ApiError(401, "invalid_credentials"));
    render(<App />); const user = userEvent.setup();
    await user.type(await screen.findByLabelText("用户名"), "employee");
    await user.type(screen.getByLabelText("密码"), "wrong");
    await user.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("用户名或密码不正确，请重新输入。");
  });

  it("restores an existing session and logs out", async () => {
    vi.mocked(client.getCurrentUser).mockResolvedValue({ username: "employee", role: "employee" });
    vi.mocked(client.logout).mockResolvedValue();
    render(<App />); const user = userEvent.setup();
    expect(await screen.findByText("employee")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "退出" }));
    expect(await screen.findByRole("heading", { name: "登录企业 AI 工作台" })).toBeInTheDocument();
  });

  it("returns to login when an active API request reports session expiry", async () => {
    vi.mocked(client.getCurrentUser).mockResolvedValue({ username: "employee", role: "employee" });
    render(<App />);
    expect(await screen.findByText("employee")).toBeInTheDocument();

    window.dispatchEvent(new Event("policy-session-expired"));

    expect(await screen.findByRole("heading", { name: "登录企业 AI 工作台" })).toBeInTheDocument();
  });
});
