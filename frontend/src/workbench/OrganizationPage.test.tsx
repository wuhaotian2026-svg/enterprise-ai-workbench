import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/request";
import * as workbenchClient from "./client";
import { OrganizationPage } from "./OrganizationPage";

vi.mock("./client", () => ({
  listOrganizationUnits: vi.fn(),
  listOrganizationEmployees: vi.fn(),
  listCapabilityGrants: vi.fn(),
  createOrganizationUnit: vi.fn(),
  updateOrganizationUnit: vi.fn(),
  updateEmployeeAssignment: vi.fn(),
  createCapabilityGrant: vi.fn(),
  revokeCapabilityGrant: vi.fn(),
  newWorkbenchOperationId: vi.fn(() => "00000000-0000-4000-8000-000000000901"),
}));

const root = {
  id: "10000000-0000-4000-8000-000000000001",
  code: "PRODUCT",
  name: "产品中心",
  parent_id: null,
  is_active: true,
};

const child = {
  id: "10000000-0000-4000-8000-000000000002",
  code: "PLATFORM",
  name: "平台组",
  parent_id: root.id,
  is_active: true,
};

const manager = {
  id: "20000000-0000-4000-8000-000000000001",
  user_id: "30000000-0000-4000-8000-000000000001",
  employee_number: "E-1001",
  display_name: "王经理",
  organization_unit_id: root.id,
  manager_employee_id: null,
  is_active: true,
};

const employee = {
  id: "20000000-0000-4000-8000-000000000002",
  user_id: "30000000-0000-4000-8000-000000000002",
  employee_number: "E-1002",
  display_name: "李员工",
  organization_unit_id: child.id,
  manager_employee_id: manager.id,
  is_active: true,
};

const globalGrant = {
  id: "40000000-0000-4000-8000-000000000001",
  user_id: employee.user_id,
  capability: "analytics.view",
  scope_kind: "global" as const,
  organization_unit_id: null,
  is_active: true,
};

const subtreeGrant = {
  id: "40000000-0000-4000-8000-000000000002",
  user_id: manager.user_id,
  capability: "hr.leave.review",
  scope_kind: "unit_subtree" as const,
  organization_unit_id: root.id,
  is_active: true,
};

beforeEach(() => {
  vi.mocked(workbenchClient.newWorkbenchOperationId)
    .mockReset()
    .mockReturnValue("00000000-0000-4000-8000-000000000901");
  vi.mocked(workbenchClient.listOrganizationUnits).mockResolvedValue([root, child]);
  vi.mocked(workbenchClient.listOrganizationEmployees).mockResolvedValue([
    manager,
    employee,
  ]);
  vi.mocked(workbenchClient.listCapabilityGrants).mockResolvedValue([
    globalGrant,
    subtreeGrant,
  ]);
  vi.mocked(workbenchClient.createOrganizationUnit).mockResolvedValue(root);
  vi.mocked(workbenchClient.updateEmployeeAssignment).mockResolvedValue(employee);
  vi.mocked(workbenchClient.revokeCapabilityGrant).mockResolvedValue({
    ...globalGrant,
    is_active: false,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("enterprise organization workspace", () => {
  it("loads the hierarchy, employee assignments, and explicit grant scopes", async () => {
    render(<OrganizationPage />);

    expect(screen.getByRole("status")).toHaveTextContent("正在读取企业组织");
    expect(
      await screen.findByRole("heading", { name: "企业组织" }),
    ).toBeInTheDocument();
    const tree = screen.getByLabelText("组织树");
    expect(within(tree).getByText("产品中心")).toBeInTheDocument();
    expect(within(tree).getByText("平台组")).toBeInTheDocument();
    expect(
      within(screen.getByRole("table", { name: "员工归属列表" })).getByText(
        "李员工",
      ),
    ).toBeInTheDocument();
    const grants = screen.getByRole("table", { name: "能力授权列表" });
    expect(within(grants).getByText("全企业")).toBeInTheDocument();
    expect(within(grants).getByText("产品中心及下级组织")).toBeInTheDocument();
  });

  it("shows an honest empty state without exposing sensitive administration", async () => {
    vi.mocked(workbenchClient.listOrganizationUnits).mockResolvedValue([]);
    vi.mocked(workbenchClient.listOrganizationEmployees).mockResolvedValue([]);
    vi.mocked(workbenchClient.listCapabilityGrants).mockResolvedValue([]);

    render(<OrganizationPage />);

    expect(await screen.findByText("尚未建立企业组织")).toBeInTheDocument();
    expect(screen.queryByText(/密码/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/provider/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/session/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/json/i)).not.toBeInTheDocument();
  });

  it("opens a department as read-only details before exposing edit actions", async () => {
    const user = userEvent.setup();
    render(<OrganizationPage />);
    await screen.findByRole("heading", { name: "企业组织" });

    await user.click(
      within(screen.getByLabelText("组织树")).getByRole("button", {
        name: /平台组/,
      }),
    );

    const detail = screen.getByRole("dialog", { name: "平台组" });
    expect(detail).toHaveAttribute("aria-modal", "true");
    expect(within(detail).getByText("PLATFORM")).toBeInTheDocument();
    expect(within(detail).getByText("产品中心")).toBeInTheDocument();
    expect(within(detail).getByText("启用")).toBeInTheDocument();
    expect(within(detail).getByText("1 人")).toBeInTheDocument();
    expect(
      within(detail).queryByRole("button", { name: "保存变更" }),
    ).not.toBeInTheDocument();
    expect(
      within(detail).queryByRole("button", { name: "停用部门" }),
    ).not.toBeInTheDocument();

    await user.click(
      within(detail).getByRole("button", { name: "编辑部门" }),
    );

    const editor = screen.getByRole("dialog", { name: "编辑部门" });
    expect(within(editor).getByLabelText("部门名称")).toHaveValue("平台组");
    expect(
      within(editor).getByRole("button", { name: "保存变更" }),
    ).toBeInTheDocument();
    expect(
      within(editor).getByRole("button", { name: "停用部门" }),
    ).toBeInTheDocument();
  });

  it("traps department-dialog focus and restores it after Escape", async () => {
    const user = userEvent.setup();
    render(<OrganizationPage />);
    await screen.findByRole("heading", { name: "企业组织" });
    const trigger = within(screen.getByLabelText("组织树")).getByRole(
      "button",
      { name: /产品中心/ },
    );

    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "产品中心" });
    const close = within(dialog).getByRole("button", {
      name: "关闭部门详情",
    });
    const edit = within(dialog).getByRole("button", { name: "编辑部门" });
    await waitFor(() => expect(dialog).toContainElement(
      document.activeElement as HTMLElement | null,
    ));

    edit.focus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(edit).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "产品中心" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("gives create employee and revoke drawers modal Escape and focus restoration", async () => {
    const user = userEvent.setup();
    render(<OrganizationPage />);
    await screen.findByRole("heading", { name: "企业组织" });

    const createTrigger = screen.getByRole("button", { name: "新增部门" });
    await user.click(createTrigger);
    const createDialog = screen.getByRole("dialog", { name: "新增部门" });
    expect(createDialog).toHaveAttribute("aria-modal", "true");
    await waitFor(() => expect(createDialog).toContainElement(
      document.activeElement as HTMLElement | null,
    ));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "新增部门" })).not.toBeInTheDocument();
    expect(createTrigger).toHaveFocus();

    const employeeTrigger = screen.getByRole("button", { name: "编辑 李员工" });
    await user.click(employeeTrigger);
    const employeeDialog = screen.getByRole("dialog", { name: "李员工" });
    expect(employeeDialog).toHaveAttribute("aria-modal", "true");
    await waitFor(() => expect(employeeDialog).toContainElement(
      document.activeElement as HTMLElement | null,
    ));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "李员工" })).not.toBeInTheDocument();
    expect(employeeTrigger).toHaveFocus();

    const revokeTrigger = screen.getByRole("button", {
      name: "撤销 analytics.view 授权",
    });
    await user.click(revokeTrigger);
    const revokeDialog = screen.getByRole("dialog", { name: "撤销授权" });
    expect(revokeDialog).toHaveAttribute("aria-modal", "true");
    await waitFor(() => expect(revokeDialog).toContainElement(
      document.activeElement as HTMLElement | null,
    ));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "撤销授权" })).not.toBeInTheDocument();
    expect(revokeTrigger).toHaveFocus();
  });

  it("creates a department and updates an employee with stable operations", async () => {
    const user = userEvent.setup();
    render(<OrganizationPage />);
    await screen.findByRole("heading", { name: "企业组织" });

    await user.click(screen.getByRole("button", { name: "新增部门" }));
    await user.type(screen.getByLabelText("部门编码"), "FINANCE");
    await user.type(screen.getByLabelText("部门名称"), "财务中心");
    await user.click(screen.getByRole("button", { name: "保存部门" }));
    expect(workbenchClient.createOrganizationUnit).toHaveBeenCalledWith({
      client_operation_id: "00000000-0000-4000-8000-000000000901",
      code: "FINANCE",
      name: "财务中心",
      parent_id: null,
    });

    await user.click(screen.getByRole("button", { name: "编辑 李员工" }));
    await user.selectOptions(screen.getByLabelText("主组织"), root.id);
    await user.selectOptions(screen.getByLabelText("直属经理"), manager.id);
    await user.click(screen.getByRole("button", { name: "保存员工归属" }));
    expect(workbenchClient.updateEmployeeAssignment).toHaveBeenCalledWith(
      employee.id,
      {
        client_operation_id: "00000000-0000-4000-8000-000000000901",
        organization_unit_id: root.id,
        manager_employee_id: manager.id,
      },
    );
  });

  it("reuses the department update operation after an uncertain failure", async () => {
    vi.mocked(workbenchClient.newWorkbenchOperationId)
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000911")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000912");
    vi.mocked(workbenchClient.updateOrganizationUnit)
      .mockRejectedValueOnce(new Error("network interrupted"))
      .mockResolvedValueOnce(root);
    const user = userEvent.setup();
    render(<OrganizationPage />);
    await screen.findByRole("heading", { name: "企业组织" });

    await user.click(
      within(screen.getByLabelText("组织树")).getByRole("button", {
        name: /产品中心/,
      }),
    );
    await user.click(screen.getByRole("button", { name: "编辑部门" }));
    await user.click(screen.getByRole("button", { name: "保存变更" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "企业组织操作暂时未完成，请稍后重试",
    );

    await user.click(screen.getByRole("button", { name: "保存变更" }));
    await waitFor(() => {
      expect(workbenchClient.updateOrganizationUnit).toHaveBeenCalledTimes(2);
    });

    const firstPayload = vi.mocked(workbenchClient.updateOrganizationUnit).mock
      .calls[0]?.[1];
    const retryPayload = vi.mocked(workbenchClient.updateOrganizationUnit).mock
      .calls[1]?.[1];
    expect(firstPayload?.client_operation_id).toBe(
      "00000000-0000-4000-8000-000000000911",
    );
    expect(retryPayload?.client_operation_id).toBe(
      firstPayload?.client_operation_id,
    );
  });

  it("renews a department-create operation when its payload changes", async () => {
    vi.mocked(workbenchClient.newWorkbenchOperationId)
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000921")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000922");
    vi.mocked(workbenchClient.createOrganizationUnit)
      .mockRejectedValueOnce(new Error("network interrupted"))
      .mockResolvedValueOnce(root);
    const user = userEvent.setup();
    render(<OrganizationPage />);
    await screen.findByRole("heading", { name: "企业组织" });

    await user.click(screen.getByRole("button", { name: "新增部门" }));
    await user.type(screen.getByLabelText("部门编码"), "FINANCE");
    await user.type(screen.getByLabelText("部门名称"), "财务中心");
    await user.click(screen.getByRole("button", { name: "保存部门" }));
    await screen.findByRole("alert");

    await user.clear(screen.getByLabelText("部门名称"));
    await user.type(screen.getByLabelText("部门名称"), "财务共享中心");
    await user.click(screen.getByRole("button", { name: "保存部门" }));
    await waitFor(() => {
      expect(workbenchClient.createOrganizationUnit).toHaveBeenCalledTimes(2);
    });

    const calls = vi.mocked(workbenchClient.createOrganizationUnit).mock.calls;
    expect(calls[0]?.[0].client_operation_id).toBe(
      "00000000-0000-4000-8000-000000000921",
    );
    expect(calls[1]?.[0].client_operation_id).toBe(
      "00000000-0000-4000-8000-000000000922",
    );
  });

  it("renews an employee-assignment operation when its payload changes", async () => {
    vi.mocked(workbenchClient.newWorkbenchOperationId)
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000931")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000932");
    vi.mocked(workbenchClient.updateEmployeeAssignment)
      .mockRejectedValueOnce(new Error("network interrupted"))
      .mockResolvedValueOnce(employee);
    const user = userEvent.setup();
    render(<OrganizationPage />);
    await screen.findByRole("heading", { name: "企业组织" });

    await user.click(screen.getByRole("button", { name: "编辑 李员工" }));
    await user.selectOptions(screen.getByLabelText("主组织"), root.id);
    await user.click(screen.getByRole("button", { name: "保存员工归属" }));
    await screen.findByRole("alert");

    await user.selectOptions(screen.getByLabelText("主组织"), child.id);
    await user.click(screen.getByRole("button", { name: "保存员工归属" }));
    await waitFor(() => {
      expect(workbenchClient.updateEmployeeAssignment).toHaveBeenCalledTimes(2);
    });

    const calls = vi.mocked(workbenchClient.updateEmployeeAssignment).mock.calls;
    expect(calls[0]?.[1].client_operation_id).toBe(
      "00000000-0000-4000-8000-000000000931",
    );
    expect(calls[1]?.[1].client_operation_id).toBe(
      "00000000-0000-4000-8000-000000000932",
    );
  });

  it("renews a capability-grant operation when its payload changes", async () => {
    vi.mocked(workbenchClient.newWorkbenchOperationId)
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000941")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000942");
    vi.mocked(workbenchClient.createCapabilityGrant)
      .mockRejectedValueOnce(new Error("network interrupted"))
      .mockResolvedValueOnce(globalGrant);
    const user = userEvent.setup();
    render(<OrganizationPage />);
    await screen.findByRole("heading", { name: "企业组织" });

    await user.selectOptions(screen.getByLabelText("授权员工"), employee.user_id);
    await user.click(screen.getByRole("button", { name: "添加授权" }));
    await screen.findByRole("alert");

    await user.selectOptions(screen.getByLabelText("能力"), "knowledge.manage");
    await user.click(screen.getByRole("button", { name: "添加授权" }));
    await waitFor(() => {
      expect(workbenchClient.createCapabilityGrant).toHaveBeenCalledTimes(2);
    });

    const calls = vi.mocked(workbenchClient.createCapabilityGrant).mock.calls;
    expect(calls[0]?.[0].client_operation_id).toBe(
      "00000000-0000-4000-8000-000000000941",
    );
    expect(calls[1]?.[0].client_operation_id).toBe(
      "00000000-0000-4000-8000-000000000942",
    );
  });

  it("requires a second confirmation for revoke and maps safe errors", async () => {
    const user = userEvent.setup();
    render(<OrganizationPage />);
    await screen.findByRole("heading", { name: "企业组织" });

    await user.click(
      screen.getByRole("button", { name: "撤销 analytics.view 授权" }),
    );
    const confirm = screen.getByRole("button", { name: "确认撤销授权" });
    expect(confirm).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "我已确认撤销影响" }));
    await user.click(confirm);
    expect(workbenchClient.revokeCapabilityGrant).toHaveBeenCalledWith(
      globalGrant.id,
      "00000000-0000-4000-8000-000000000901",
    );

    vi.mocked(workbenchClient.createOrganizationUnit).mockRejectedValueOnce(
      new ApiError(409, "organization_unit_cycle"),
    );
    await user.click(screen.getByRole("button", { name: "新增部门" }));
    await user.type(screen.getByLabelText("部门编码"), "CYCLE");
    await user.type(screen.getByLabelText("部门名称"), "循环组织");
    await user.click(screen.getByRole("button", { name: "保存部门" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("组织层级不能形成循环");
  });

  it("fails closed on permission errors and retries loading explicitly", async () => {
    vi.mocked(workbenchClient.listOrganizationUnits)
      .mockRejectedValueOnce(new ApiError(403, "capability_required"))
      .mockResolvedValueOnce([root, child]);
    const user = userEvent.setup();
    render(<OrganizationPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "当前账号没有企业组织管理权限",
    );
    await user.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      expect(workbenchClient.listOrganizationUnits).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByLabelText("组织树")).toBeInTheDocument();
  });
});
