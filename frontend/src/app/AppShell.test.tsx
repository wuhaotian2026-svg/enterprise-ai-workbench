import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { listWorkbenchModules, recordModuleOpened } from "../workbench/client";
import { AppShell } from "./AppShell";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    listQuestions: vi.fn().mockResolvedValue([]),
    listDocuments: vi.fn().mockResolvedValue([]),
  };
});

vi.mock("../hr/client", () => ({
  listConversations: vi.fn().mockResolvedValue([]),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  newClientId: vi.fn(() => crypto.randomUUID()),
  sendTurn: vi.fn(),
  confirmTool: vi.fn(),
  cancelTool: vi.fn(),
  listLeaveRequests: vi.fn().mockResolvedValue([]),
  createCancelIntent: vi.fn(),
  listReviewQueue: vi.fn().mockResolvedValue([]),
  getReviewDetail: vi.fn(),
  approveLeaveRequest: vi.fn(),
  rejectLeaveRequest: vi.fn(),
}));

vi.mock("../workbench/client", () => ({
  listWorkbenchModules: vi.fn(),
  recordModuleOpened: vi.fn().mockResolvedValue(undefined),
  listOrganizationUnits: vi.fn().mockResolvedValue([]),
  listOrganizationEmployees: vi.fn().mockResolvedValue([]),
  listCapabilityGrants: vi.fn().mockResolvedValue([]),
  loadAnalyticsDashboard: vi.fn(() => new Promise(() => {})),
  createOrganizationUnit: vi.fn(),
  updateOrganizationUnit: vi.fn(),
  updateEmployeeAssignment: vi.fn(),
  createCapabilityGrant: vi.fn(),
  revokeCapabilityGrant: vi.fn(),
  newWorkbenchOperationId: vi.fn(() => crypto.randomUUID()),
}));

const listModules = vi.mocked(listWorkbenchModules);
const recordOpened = vi.mocked(recordModuleOpened);

function module(key: string, label: string, index: string) {
  return { key, label, index };
}

const employeeModules = {
  catalog_version: "2026-08-16" as const,
  modules: [
    module("knowledge", "制度知识库", "01"),
    module("hr-assistant", "请假助手", "02"),
    module("my-requests", "我的申请", "03"),
  ],
};

beforeEach(() => {
  listModules.mockResolvedValue(employeeModules);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function moduleNames(): string[] {
  return screen
    .getAllByRole("button", { name: /^(制度知识库|请假助手|我的申请|HR 审核|知识管理|企业组织|运营驾驶舱|采购申请|审批中心)$/ })
    .map((item) => item.getAttribute("aria-label") ?? "");
}

describe("server-authorized enterprise workbench shell", () => {
  it("uses the unified enterprise workbench brand", async () => {
    render(
      <AppShell
        user={{ username: "alice.hr.demo", role: "employee" }}
        onLogout={vi.fn()}
      />,
    );

    expect(screen.getByText("ENTERPRISE / AI WORKBENCH")).toBeInTheDocument();
    expect(screen.queryByText(/FDE/)).not.toBeInTheDocument();
    await screen.findByRole("button", { name: "制度知识库" });
  });

  it("records only real active module transitions", async () => {
    const user = userEvent.setup();
    render(
      <AppShell
        user={{ username: "alice.hr.demo", role: "employee" }}
        onLogout={vi.fn()}
      />,
    );

    await screen.findByRole("button", { name: "制度知识库" });
    await waitFor(() => expect(recordOpened).toHaveBeenCalledTimes(1));
    expect(recordOpened).toHaveBeenLastCalledWith("knowledge");

    await user.click(screen.getByRole("button", { name: "制度知识库" }));
    expect(recordOpened).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "请假助手" }));
    await waitFor(() => expect(recordOpened).toHaveBeenCalledTimes(2));
    expect(recordOpened).toHaveBeenLastCalledWith("hr-assistant");

    await user.click(screen.getByRole("button", { name: "请假助手" }));
    expect(recordOpened).toHaveBeenCalledTimes(2);
  });

  it("shows only employee modules and preserves identity while switching", async () => {
    const logout = vi.fn();
    const user = userEvent.setup();
    render(
      <AppShell
        user={{ username: "alice.hr.demo", role: "employee" }}
        onLogout={logout}
      />,
    );

    expect(screen.getByText("正在加载工作台模块…")).toBeInTheDocument();
    await screen.findByRole("button", { name: "制度知识库" });
    expect(moduleNames()).toEqual(["制度知识库", "请假助手", "我的申请"]);
    expect(await screen.findByRole("heading", { name: /从制度原文开始/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "请假助手" }));
    expect(screen.getByRole("heading", { name: /把制度查询/ })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "向 AI 请假助手说明事项" })).toBeInTheDocument();
    expect(screen.getByText("alice.hr.demo")).toBeInTheDocument();
    expect(logout).not.toHaveBeenCalled();
  });

  it("gives HR review access without knowledge administration", async () => {
    listModules.mockResolvedValue({
      catalog_version: "2026-08-16",
      modules: [
        ...employeeModules.modules,
        module("hr-review", "HR 审核", "04"),
      ],
    });
    const user = userEvent.setup();
    render(
      <AppShell
        user={{ username: "helen.hr.demo", role: "hr" }}
        onLogout={vi.fn()}
      />,
    );

    await screen.findByRole("button", { name: "HR 审核" });
    expect(moduleNames()).toEqual([
      "制度知识库",
      "请假助手",
      "我的申请",
      "HR 审核",
    ]);
    expect(screen.queryByRole("button", { name: "知识管理" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "HR 审核" }));
    expect(screen.getByRole("heading", { name: "HR 审核" })).toBeInTheDocument();
    expect(await screen.findByText(/当前没有待处理申请/)).toBeInTheDocument();
  });

  it("keeps administrators out of HR review and preserves the existing admin page", async () => {
    listModules.mockResolvedValue({
      catalog_version: "2026-08-16",
      modules: [
        module("knowledge", "制度知识库", "01"),
        module("knowledge-admin", "知识管理", "05"),
        module("organization", "企业组织", "06"),
        module("analytics", "运营驾驶舱", "07"),
      ],
    });
    const user = userEvent.setup();
    render(
      <AppShell
        user={{ username: "admin.hr.demo", role: "admin" }}
        onLogout={vi.fn()}
      />,
    );

    await screen.findByRole("button", { name: "制度知识库" });
    expect(moduleNames()).toEqual([
      "制度知识库",
      "知识管理",
      "企业组织",
      "运营驾驶舱",
    ]);
    expect(screen.queryByRole("button", { name: "HR 审核" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "企业组织" }));
    expect(await screen.findByRole("heading", { name: "企业组织" })).toBeInTheDocument();
    expect(screen.getByText("尚未建立企业组织")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "运营驾驶舱" }));
    expect(
      await screen.findByRole("heading", { name: "运营驾驶舱" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "知识管理" }));
    expect(await screen.findByRole("heading", { name: "制度资料管理" })).toBeInTheDocument();
  });

  it("ignores server keys that are not registered in the compiled frontend", async () => {
    listModules.mockResolvedValue({
      catalog_version: "2026-08-16",
      modules: [
        module("knowledge", "制度知识库", "01"),
        module("server-injected-component", "Injected", "99"),
      ],
    });

    render(
      <AppShell
        user={{ username: "alice.hr.demo", role: "employee" }}
        onLogout={vi.fn()}
      />,
    );

    await screen.findByRole("button", { name: "制度知识库" });
    await waitFor(() => expect(recordOpened).toHaveBeenCalledTimes(1));
    expect(recordOpened).toHaveBeenCalledWith("knowledge");
    expect(moduleNames()).toEqual(["制度知识库"]);
    expect(screen.queryByText("Injected")).not.toBeInTheDocument();
  });

  it("registers the 2026-08-23 procurement modules only when the server returns them", async () => {
    listModules.mockResolvedValue({
      catalog_version: "2026-08-23",
      modules: [
        module("knowledge", "制度知识库", "01"),
        module("procurement", "采购申请", "08"),
        module("approval-center", "审批中心", "09"),
      ],
    });
    const user = userEvent.setup();

    render(
      <AppShell
        user={{ username: "employee.with.explicit.grants", role: "employee" }}
        onLogout={vi.fn()}
      />,
    );

    await screen.findByRole("button", { name: "采购申请" });
    expect(moduleNames()).toEqual(["制度知识库", "采购申请", "审批中心"]);
    await user.click(screen.getByRole("button", { name: "采购申请" }));
    expect(screen.getByRole("heading", { name: "采购申请" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "审批中心" }));
    expect(screen.getByRole("heading", { name: "审批中心" })).toBeInTheDocument();
  });

  it("does not expose a compiled procurement module that the server omitted", async () => {
    listModules.mockResolvedValue({
      catalog_version: "2026-08-23",
      modules: [module("procurement", "采购申请", "08")],
    });

    render(
      <AppShell
        user={{ username: "admin.without.approval.grant", role: "admin" }}
        onLogout={vi.fn()}
      />,
    );

    await screen.findByRole("button", { name: "采购申请" });
    expect(moduleNames()).toEqual(["采购申请"]);
    expect(screen.queryByRole("button", { name: "审批中心" })).not.toBeInTheDocument();
  });

  it("fails closed when module loading fails and retries explicitly", async () => {
    listModules.mockRejectedValueOnce(new Error("network unavailable"));
    const user = userEvent.setup();
    render(
      <AppShell
        user={{ username: "admin.hr.demo", role: "admin" }}
        onLogout={vi.fn()}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("工作台模块加载失败");
    expect(screen.queryByRole("button", { name: "知识管理" })).not.toBeInTheDocument();

    listModules.mockResolvedValueOnce(employeeModules);
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("button", { name: "制度知识库" })).toBeInTheDocument();
    expect(listModules).toHaveBeenCalledTimes(2);
  });

  it("shows an explicit empty state when the actor has no registered modules", async () => {
    listModules.mockResolvedValue({
      catalog_version: "2026-08-16",
      modules: [],
    });

    render(
      <AppShell
        user={{ username: "inactive.hr.demo", role: "employee" }}
        onLogout={vi.fn()}
      />,
    );

    expect(await screen.findByText("当前账号没有可用的工作台模块")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "制度知识库" })).not.toBeInTheDocument();
  });

  it("preserves the active module when a refreshed catalog still allows it", async () => {
    const actor = {
      username: "alice.hr.demo",
      role: "employee" as const,
    };
    const user = userEvent.setup();
    const view = render(<AppShell user={actor} onLogout={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "请假助手" }));
    expect(screen.getByRole("heading", { name: /把制度查询/ })).toBeInTheDocument();

    listModules.mockResolvedValueOnce(employeeModules);
    view.rerender(
      <AppShell
        user={{ ...actor, role: "hr" }}
        onLogout={vi.fn()}
      />,
    );

    await waitFor(() => expect(listModules).toHaveBeenCalledTimes(2));
    await screen.findByRole("button", { name: "请假助手" });
    expect(screen.getByRole("heading", { name: /把制度查询/ })).toBeInTheDocument();
  });

  it("exposes a button-controlled mobile module drawer", async () => {
    const user = userEvent.setup();
    render(
      <AppShell
        user={{ username: "alice.hr.demo", role: "employee" }}
        onLogout={vi.fn()}
      />,
    );
    await screen.findByRole("button", { name: "制度知识库" });
    const trigger = screen.getByRole("button", { name: "打开工作台导航" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    await user.click(screen.getByRole("button", { name: "我的申请" }));
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("heading", { name: "我的申请" })).toBeInTheDocument();
  });
});
