import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/request";
import { ApprovalCenterPage } from "./ApprovalCenterPage";
import * as approvalClient from "./client";
import type {
  ApprovalTaskDetail,
  ApprovalTaskPage,
  ApprovalTaskStatus,
  ApprovalTaskSummary,
} from "./types";

vi.mock("./client", () => ({
  listApprovalTasks: vi.fn(),
  getApprovalTask: vi.fn(),
  approveTask: vi.fn(),
  rejectTask: vi.fn(),
  newApprovalOperationId: vi.fn(),
}));

const TASK_ID = "11111111-1111-4111-8111-111111111111";
const INSTANCE_ID = "22222222-2222-4222-8222-222222222222";
const OTHER_TASK_ID = "33333333-3333-4333-8333-333333333333";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

function task(status: ApprovalTaskStatus = "pending", taskId = TASK_ID): ApprovalTaskSummary {
  return {
    task_id: taskId,
    instance_id: INSTANCE_ID,
    process_key: "procurement.request",
    subject_type: "procurement_request",
    step_key: status === "waiting" ? "procurement_review" : "department_manager_review",
    step_label: status === "waiting" ? "采购专员复核" : "直属部门负责人审批",
    status,
    submitted_at: "2026-08-25T01:00:00Z",
    activated_at: status === "waiting" ? null : "2026-08-25T02:00:00Z",
    completed_at: status === "pending" || status === "waiting" ? null : "2026-08-25T03:00:00Z",
    subject: {
      supported: true,
      request_number: "PR-20260825-0001",
      title: "研发工作站采购",
      total: "16888.00",
      status: status === "waiting" ? "pending_manager" : "pending_procurement",
    },
  };
}

function page(items = [task()], offset = 0, total = items.length): ApprovalTaskPage {
  return { items, offset, limit: 10, total };
}

function detail(status: ApprovalTaskStatus = "pending", taskId = TASK_ID): ApprovalTaskDetail {
  const taskValue = task(status, taskId);
  if (!taskValue.subject.supported) throw new Error("test fixture must be supported");
  return {
    task: taskValue,
    subject: {
      supported: true,
      summary: taskValue.subject,
      purpose: "用于本地模型验证，不包含任何审批建议。",
      needed_by_date: "2026-09-30",
      currency: "CNY",
      applicant: { display_name: "林岚" },
      organization: { display_name: "研发中心" },
      items: [{
        category: "it_equipment",
        name: "<img src=x onerror=alert(1)>",
        specification: "64GB / 2TB",
        quantity: "1.00",
        unit: "台",
        unit_price: "16888.00",
        subtotal: "16888.00",
      }],
      timeline: [
        {
          kind: "submitted", occurred_at: "2026-08-25T01:00:00Z", step_key: null,
          step_label: null, action: null, actor_display_name: "林岚", comment: null, status: "submitted",
        },
        {
          kind: "decision", occurred_at: "2026-08-25T02:30:00Z",
          step_key: "department_manager_review", step_label: "直属部门负责人审批",
          action: "approve", actor_display_name: "周经理", comment: "业务需要明确", status: "approved",
        },
        {
          kind: "decision", occurred_at: "2026-08-25T03:00:00Z",
          step_key: "procurement_review", step_label: "采购专员复核",
          action: null, actor_display_name: null, comment: null, status: "pending",
        },
      ],
    },
  };
}

function namedTask(taskId: string, title: string, total: string): ApprovalTaskSummary {
  const value = task("pending", taskId);
  if (!value.subject.supported) throw new Error("test fixture must be supported");
  value.subject = { ...value.subject, request_number: `PR-${taskId.slice(0, 8)}`, title, total };
  return value;
}

function namedDetail(taskId: string, title: string, total: string, purpose: string): ApprovalTaskDetail {
  const value = detail("pending", taskId);
  const summary = namedTask(taskId, title, total);
  if (!summary.subject.supported || !value.subject.supported) throw new Error("test fixture must be supported");
  return {
    task: summary,
    subject: { ...value.subject, summary: summary.subject, purpose },
  };
}

async function openTask(user = userEvent.setup()) {
  render(<ApprovalCenterPage />);
  await screen.findByText("研发工作站采购");
  await user.click(screen.getByRole("button", { name: /研发工作站采购/ }));
  await screen.findByRole("dialog", { name: /采购申请 PR-20260825-0001/ });
  return user;
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(approvalClient.listApprovalTasks).mockResolvedValue(page());
  vi.mocked(approvalClient.getApprovalTask).mockResolvedValue(detail());
  vi.mocked(approvalClient.approveTask).mockResolvedValue({
    instance_id: INSTANCE_ID, status: "running", current_step_key: "procurement_review", replayed: false,
  });
  vi.mocked(approvalClient.rejectTask).mockResolvedValue({
    instance_id: INSTANCE_ID, status: "rejected", current_step_key: null, replayed: false,
  });
  let operation = 0;
  vi.mocked(approvalClient.newApprovalOperationId).mockImplementation(
    () => `aaaaaaaa-aaaa-4aaa-8aaa-${String(++operation).padStart(12, "0")}`,
  );
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("ApprovalCenterPage", () => {
  it("loads the pending queue by default and uses server pagination", async () => {
    vi.mocked(approvalClient.listApprovalTasks)
      .mockResolvedValueOnce(page([task()], 0, 11))
      .mockResolvedValueOnce(page([task("pending", OTHER_TASK_ID)], 10, 11));
    const user = userEvent.setup();
    render(<ApprovalCenterPage />);
    await screen.findByText("研发工作站采购");
    expect(approvalClient.listApprovalTasks).toHaveBeenNthCalledWith(1, expect.objectContaining({
      status: "pending", offset: 0, limit: 10, signal: expect.any(AbortSignal),
    }));
    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(approvalClient.listApprovalTasks).toHaveBeenNthCalledWith(
      2, expect.objectContaining({ status: "pending", offset: 10, limit: 10 }),
    ));
  });

  it("supports completed status, business type, and date filters", async () => {
    const user = userEvent.setup();
    render(<ApprovalCenterPage />);
    await screen.findByText("研发工作站采购");
    await user.click(screen.getByRole("button", { name: "已处理" }));
    await user.selectOptions(screen.getByLabelText("任务状态"), "rejected");
    await user.selectOptions(screen.getByLabelText("业务类型"), "procurement.request");
    await user.type(screen.getByLabelText("激活日期从"), "2026-08-01");
    await user.type(screen.getByLabelText("激活日期至"), "2026-08-31");
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => expect(approvalClient.listApprovalTasks).toHaveBeenLastCalledWith(
      expect.objectContaining({
        status: "rejected", processKey: "procurement.request", offset: 0,
        activatedFrom: "2026-08-01T00:00:00+08:00",
        activatedTo: "2026-08-31T23:59:59+08:00",
      }),
    ));
  });

  it("rejects an inverted activation date range without issuing a request", async () => {
    const user = userEvent.setup();
    render(<ApprovalCenterPage />);
    await screen.findByText("研发工作站采购");
    vi.mocked(approvalClient.listApprovalTasks).mockClear();
    await user.type(screen.getByLabelText("激活日期从"), "2026-09-01");
    await user.type(screen.getByLabelText("激活日期至"), "2026-08-31");
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("激活日期范围无效");
    expect(approvalClient.listApprovalTasks).not.toHaveBeenCalled();
  });

  it("renders procurement facts and a two-level timeline as text without amount advice", async () => {
    await openTask();
    const dialog = screen.getByRole("dialog", { name: /采购申请 PR-20260825-0001/ });
    expect(within(dialog).getByText("用于本地模型验证，不包含任何审批建议。")).toBeInTheDocument();
    expect(within(dialog).getByText("组织")).toBeInTheDocument();
    expect(within(dialog).getByText("研发中心")).toBeInTheDocument();
    expect(within(dialog).getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(dialog.querySelector("img")).toBeNull();
    expect(within(dialog).getAllByText("直属部门负责人审批").length).toBeGreaterThanOrEqual(1);
    expect(within(dialog).getByText("采购专员复核")).toBeInTheDocument();
    expect(screen.queryByText(/建议批准|建议拒绝|金额较高/)).not.toBeInTheDocument();
  });

  it("fails closed for unknown subjects without dumping payload", async () => {
    const unknown = task();
    unknown.process_key = "contract.request";
    unknown.subject_type = "contract_request";
    unknown.subject = {
      supported: false,
      process_key: "contract.request",
      subject_type: "contract_request",
    };
    vi.mocked(approvalClient.listApprovalTasks).mockResolvedValue(page([unknown]));
    vi.mocked(approvalClient.getApprovalTask).mockResolvedValue({
      task: unknown,
      subject: { supported: false, process_key: "contract.request", subject_type: "contract_request" },
    });
    const user = userEvent.setup();
    render(<ApprovalCenterPage />);
    await screen.findByText("不支持的业务类型");
    await user.click(screen.getByRole("button", { name: /不支持的业务类型/ }));
    expect(await screen.findByText("当前版本暂不支持此业务类型。" )).toBeInTheDocument();
    expect(screen.queryByText(/contract_request|contract\.request|payload|secret/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "批准" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "拒绝" })).not.toBeInTheDocument();
  });

  it("does not show decision actions for a waiting task", async () => {
    vi.mocked(approvalClient.listApprovalTasks).mockResolvedValue(page([task("waiting")]));
    vi.mocked(approvalClient.getApprovalTask).mockResolvedValue(detail("waiting"));
    await openTask();
    expect(screen.getByText("等待前序步骤完成")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "批准" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "拒绝" })).not.toBeInTheDocument();
  });

  it("requires a second confirmation for approval and restores focus on Escape", async () => {
    const user = await openTask();
    const approve = screen.getByRole("button", { name: "批准" });
    approve.focus();
    await user.click(approve);
    const confirmation = screen.getByRole("dialog", { name: "批准审批任务确认" });
    const detailDialog = screen.getByRole("dialog", { name: /采购申请 PR-20260825-0001/, hidden: true });
    expect(confirmation).toHaveAttribute("aria-modal", "true");
    expect(detailDialog).toHaveAttribute("inert");
    expect(screen.getAllByRole("dialog").filter(item => item.getAttribute("aria-modal") === "true")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "确认批准" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "批准审批任务确认" })).not.toBeInTheDocument();
    expect(approve).toHaveFocus();
    expect(approvalClient.approveTask).not.toHaveBeenCalled();
  });

  it("focuses the detail dialog and closes it with Escape while restoring the queue trigger", async () => {
    const user = userEvent.setup();
    render(<ApprovalCenterPage />);
    await screen.findByText("研发工作站采购");
    const trigger = screen.getByRole("button", { name: /研发工作站采购/ });
    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: /采购申请 PR-20260825-0001/ });
    expect(dialog).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: /采购申请 PR-20260825-0001/ })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("provides a visible detail close control and restores the queue trigger", async () => {
    const user = userEvent.setup();
    render(<ApprovalCenterPage />);
    await screen.findByText("研发工作站采购");
    const trigger = screen.getByRole("button", { name: /研发工作站采购/ });
    await user.click(trigger);
    await screen.findByRole("dialog", { name: /采购申请 PR-20260825-0001/ });
    await user.click(screen.getByRole("button", { name: "关闭审批详情" }));
    expect(screen.queryByRole("dialog", { name: /采购申请 PR-20260825-0001/ })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("traps Tab inside the active confirmation dialog", async () => {
    const user = await openTask();
    await user.click(screen.getByRole("button", { name: "批准" }));
    const confirm = screen.getByRole("button", { name: "确认批准" });
    const cancel = screen.getByRole("button", { name: "取消" });
    cancel.focus();
    await user.tab();
    expect(confirm).toHaveFocus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(cancel).toHaveFocus();
  });

  it("keeps reject confirmation disabled until the reason is non-empty", async () => {
    const user = await openTask();
    await user.click(screen.getByRole("button", { name: "拒绝" }));
    const confirm = screen.getByRole("button", { name: "确认拒绝" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText("拒绝理由"), "   ");
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText("拒绝理由"), "需求依据不足");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(approvalClient.rejectTask).toHaveBeenCalledWith(
      TASK_ID, "需求依据不足", "aaaaaaaa-aaaa-4aaa-8aaa-000000000001",
    );
  });

  it("freezes the reject reason after an ambiguous attempt", async () => {
    vi.mocked(approvalClient.rejectTask).mockRejectedValueOnce(new ApiError(503, "request_failed"));
    const user = await openTask();
    await user.click(screen.getByRole("button", { name: "拒绝" }));
    await user.type(screen.getByLabelText("拒绝理由"), "需求依据不足");
    await user.click(screen.getByRole("button", { name: "确认拒绝" }));
    expect(await screen.findByLabelText("拒绝理由")).toBeDisabled();
  });

  it("keeps a busy decision open on Escape and rejects a replacement intent until settlement", async () => {
    const attempt = deferred<Awaited<ReturnType<typeof approvalClient.approveTask>>>();
    vi.mocked(approvalClient.approveTask).mockReturnValueOnce(attempt.promise);
    const user = await openTask();
    await user.click(screen.getByRole("button", { name: "批准" }));
    await user.click(screen.getByRole("button", { name: "确认批准" }));
    await waitFor(() => expect(approvalClient.approveTask).toHaveBeenCalledTimes(1));

    await user.keyboard("{Escape}");
    const approvalDialog = screen.getByRole("dialog", { name: "批准审批任务确认" });
    expect(approvalDialog).toBeInTheDocument();
    expect(within(approvalDialog).getByRole("button", { name: "取消" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "拒绝", hidden: true }));
    expect(screen.queryByRole("dialog", { name: "拒绝审批任务确认" })).not.toBeInTheDocument();

    attempt.reject(new ApiError(503, "request_failed"));
    expect(await screen.findByText(/结果暂时不明确/)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "批准审批任务确认" })).toBeInTheDocument();
  });

  it.each([
    new TypeError("network"),
    new ApiError(408, "request_failed"),
    new ApiError(429, "request_failed"),
    new ApiError(503, "request_failed"),
  ])("reuses one operation ID when an approval response is ambiguous", async failure => {
    vi.mocked(approvalClient.approveTask).mockRejectedValueOnce(failure).mockResolvedValueOnce({
      instance_id: INSTANCE_ID, status: "running", current_step_key: "procurement_review", replayed: true,
    });
    const user = await openTask();
    await user.click(screen.getByRole("button", { name: "批准" }));
    await user.click(screen.getByRole("button", { name: "确认批准" }));
    expect(await screen.findByText(/结果暂时不明确/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "使用同一操作号重试" }));
    expect(vi.mocked(approvalClient.approveTask).mock.calls[0][1])
      .toBe(vi.mocked(approvalClient.approveTask).mock.calls[1][1]);
  });

  it.each([400, 403, 404, 422])("ends an intent on deterministic HTTP %i and allocates a new operation ID", async status => {
    vi.mocked(approvalClient.approveTask)
      .mockRejectedValueOnce(new ApiError(status, "request_failed"))
      .mockResolvedValueOnce({ instance_id: INSTANCE_ID, status: "running", current_step_key: "procurement_review", replayed: false });
    const user = await openTask();
    await user.click(screen.getByRole("button", { name: "批准" }));
    await user.click(screen.getByRole("button", { name: "确认批准" }));
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "批准" }));
    await user.click(screen.getByRole("button", { name: "确认批准" }));
    expect(vi.mocked(approvalClient.approveTask).mock.calls[0][1])
      .not.toBe(vi.mocked(approvalClient.approveTask).mock.calls[1][1]);
  });

  it("reloads the authoritative queue and detail after a 409 conflict", async () => {
    vi.mocked(approvalClient.approveTask).mockRejectedValueOnce(
      new ApiError(409, "request_failed"),
    );
    const user = await openTask();
    vi.mocked(approvalClient.listApprovalTasks).mockClear();
    vi.mocked(approvalClient.getApprovalTask).mockClear();
    await user.click(screen.getByRole("button", { name: "批准" }));
    await user.click(screen.getByRole("button", { name: "确认批准" }));
    await waitFor(() => {
      expect(approvalClient.listApprovalTasks).toHaveBeenCalled();
      expect(approvalClient.getApprovalTask).toHaveBeenCalledWith(TASK_ID, expect.any(AbortSignal));
    });
    expect(screen.getByRole("alert")).toHaveTextContent("任务状态已变化，已重新加载权威状态。");
  });

  it("restores a stable queue heading after a successful state-driven detail close", async () => {
    const user = await openTask();
    await user.click(screen.getByRole("button", { name: "批准" }));
    await user.click(screen.getByRole("button", { name: "确认批准" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("审批决定已提交");
    expect(screen.getByRole("heading", { name: "待处理队列" })).toHaveFocus();
  });

  it("shows safe errors for inaccessible or missing details", async () => {
    vi.mocked(approvalClient.getApprovalTask).mockRejectedValueOnce(
      new ApiError(404, "approval_task_not_found", { detail: "private target" }),
    );
    const user = userEvent.setup();
    render(<ApprovalCenterPage />);
    await screen.findByText("研发工作站采购");
    await user.click(screen.getByRole("button", { name: /研发工作站采购/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("任务不存在或无权访问。");
    expect(screen.queryByText(/private target|approval_task_not_found/)).not.toBeInTheDocument();
  });

  it("aborts a stale list read and keeps the distinguishable newer queue result", async () => {
    let resolveOldList!: (value: ApprovalTaskPage) => void;
    const oldTask = namedTask(TASK_ID, "旧队列任务", "100.00");
    const newTask = namedTask(OTHER_TASK_ID, "新队列任务", "200.00");
    vi.mocked(approvalClient.listApprovalTasks)
      .mockImplementationOnce(() => new Promise(resolve => { resolveOldList = resolve; }))
      .mockResolvedValueOnce(page([newTask]));
    render(<ApprovalCenterPage />);
    await waitFor(() => expect(approvalClient.listApprovalTasks).toHaveBeenCalledTimes(1));
    const firstListSignal = vi.mocked(approvalClient.listApprovalTasks).mock.calls[0][0]?.signal;
    fireEvent.click(screen.getByRole("button", { name: "刷新队列" }));
    await waitFor(() => expect(approvalClient.listApprovalTasks).toHaveBeenCalledTimes(2));
    expect(firstListSignal?.aborted).toBe(true);
    expect(await screen.findByText("新队列任务")).toBeInTheDocument();
    resolveOldList(page([oldTask]));
    await waitFor(() => expect(screen.queryByText("旧队列任务")).not.toBeInTheDocument());
    expect(screen.getByText("新队列任务")).toBeInTheDocument();
  });

  it("aborts detail A and ignores its late result after detail B resolves first", async () => {
    const taskA = namedTask(TASK_ID, "任务 A", "100.00");
    const taskB = namedTask(OTHER_TASK_ID, "任务 B", "200.00");
    const detailA = deferred<ApprovalTaskDetail>();
    const detailB = deferred<ApprovalTaskDetail>();
    vi.mocked(approvalClient.listApprovalTasks).mockResolvedValue(page([taskA, taskB]));
    vi.mocked(approvalClient.getApprovalTask)
      .mockReturnValueOnce(detailA.promise)
      .mockReturnValueOnce(detailB.promise);
    const user = userEvent.setup();
    render(<ApprovalCenterPage />);
    await screen.findByText("任务 A");
    await user.click(screen.getByRole("button", { name: /任务 A/ }));
    const signalA = vi.mocked(approvalClient.getApprovalTask).mock.calls[0][1];
    await user.click(screen.getByRole("button", { name: /任务 B/ }));
    expect(signalA?.aborted).toBe(true);
    detailB.resolve(namedDetail(OTHER_TASK_ID, "任务 B", "200.00", "新详情事实"));
    expect(await screen.findByText("新详情事实")).toBeInTheDocument();
    detailA.resolve(namedDetail(TASK_ID, "任务 A", "100.00", "旧详情事实"));
    await waitFor(() => expect(screen.queryByText("旧详情事实")).not.toBeInTheDocument());
    expect(screen.getByText("新详情事实")).toBeInTheDocument();
  });

  it.each([
    { action: "mode", control: "已处理" },
    { action: "filter", control: "应用筛选" },
  ])("invalidates an in-flight detail on $action context change and keeps focus on $control", async ({ action, control }) => {
    const oldDetail = deferred<ApprovalTaskDetail>();
    vi.mocked(approvalClient.getApprovalTask).mockReturnValueOnce(oldDetail.promise);
    const user = userEvent.setup();
    render(<ApprovalCenterPage />);
    await screen.findByText("研发工作站采购");
    await user.click(screen.getByRole("button", { name: /研发工作站采购/ }));
    const detailSignal = vi.mocked(approvalClient.getApprovalTask).mock.calls[0][1];
    const contextControl = screen.getByRole("button", { name: control });
    if (action === "filter") await user.selectOptions(screen.getByLabelText("业务类型"), "procurement.request");
    await user.click(contextControl);
    expect(detailSignal?.aborted).toBe(true);
    oldDetail.resolve(detail());
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /采购申请/ })).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "批准" })).not.toBeInTheDocument();
    expect(contextControl).toHaveFocus();
  });

  it("aborts an in-flight detail when the page unmounts", async () => {
    vi.mocked(approvalClient.getApprovalTask).mockImplementationOnce(() => new Promise(() => {}));
    const view = render(<ApprovalCenterPage />);
    await screen.findByText("研发工作站采购");
    await userEvent.click(screen.getByRole("button", { name: /研发工作站采购/ }));
    const detailSignal = vi.mocked(approvalClient.getApprovalTask).mock.calls[0][1];
    view.unmount();
    expect(detailSignal?.aborted).toBe(true);
  });

  it("aborts an in-flight initial list when the page unmounts", async () => {
    vi.mocked(approvalClient.listApprovalTasks).mockImplementationOnce(() => new Promise(() => {}));
    const view = render(<ApprovalCenterPage />);
    await waitFor(() => expect(approvalClient.listApprovalTasks).toHaveBeenCalledTimes(1));
    const listSignal = vi.mocked(approvalClient.listApprovalTasks).mock.calls[0][0]?.signal;
    view.unmount();
    expect(listSignal?.aborted).toBe(true);
  });
});
