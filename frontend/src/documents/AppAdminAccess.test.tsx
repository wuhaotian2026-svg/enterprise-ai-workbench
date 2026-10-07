import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "../app/App";
import * as client from "../api/client";
import * as workbenchClient from "../workbench/client";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, getCurrentUser: vi.fn(), listQuestions: vi.fn().mockResolvedValue([]), listDocuments: vi.fn().mockResolvedValue([]) };
});
vi.mock("../workbench/client", () => ({
  listWorkbenchModules: vi.fn(),
  recordModuleOpened: vi.fn().mockResolvedValue(undefined),
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("renders document administration only for administrators", async () => {
  const user = userEvent.setup();
  vi.mocked(workbenchClient.listWorkbenchModules)
    .mockResolvedValueOnce({
      catalog_version: "2026-08-16",
      modules: [
        { key: "knowledge", label: "制度知识库", index: "01" },
        { key: "knowledge-admin", label: "知识管理", index: "05" },
        { key: "organization", label: "企业组织", index: "06" },
        { key: "analytics", label: "运营驾驶舱", index: "07" },
      ],
    })
    .mockResolvedValueOnce({
      catalog_version: "2026-08-16",
      modules: [
        { key: "knowledge", label: "制度知识库", index: "01" },
        { key: "hr-assistant", label: "HR 办事助手", index: "02" },
        { key: "my-requests", label: "我的申请", index: "03" },
      ],
    });
  vi.mocked(client.getCurrentUser).mockResolvedValue({ username: "admin", role: "admin" });
  const { unmount } = render(<App />);
  const knowledgeAdmin = await screen.findByRole("button", { name: "知识管理" });
  expect(screen.queryByRole("button", { name: "HR 审核" })).not.toBeInTheDocument();
  await user.click(knowledgeAdmin);
  expect(await screen.findByRole("heading", { name: "制度资料管理" })).toBeInTheDocument();
  unmount(); vi.mocked(client.getCurrentUser).mockResolvedValue({ username: "employee", role: "employee" });
  render(<App />);
  expect(await screen.findByRole("heading", { name: /从制度原文开始/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "HR 办事助手" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "知识管理" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "制度资料管理" })).not.toBeInTheDocument();
});
