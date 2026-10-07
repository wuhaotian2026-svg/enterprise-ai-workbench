import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/request";
import * as procurementClient from "./client";
import { ProcurementPage } from "./ProcurementPage";
import type {
  ProcurementConversationSummary,
  ProcurementRequestDetail,
  ProcurementRequestPage,
  ProcurementSubmitResult,
} from "./types";

vi.mock("./client", () => ({
  newProcurementOperationId: vi.fn(),
  listProcurementRequests: vi.fn(),
  getProcurementRequest: vi.fn(),
  previewProcurementRequest: vi.fn(),
  submitProcurementRequest: vi.fn(),
  withdrawProcurementRequest: vi.fn(),
  createProcurementConversation: vi.fn(),
  listProcurementConversations: vi.fn(),
  getProcurementConversation: vi.fn(),
  sendProcurementTurn: vi.fn(),
  confirmProcurementSubmission: vi.fn(),
  cancelProcurementConfirmation: vi.fn(),
}));

const pendingManager: ProcurementRequestDetail = {
  id: "10000000-0000-4000-8000-000000000001",
  summary: {
    request_number: "PR-2026-0001",
    title: "研发电脑",
    total: "12999.00",
    status: "pending_manager",
  },
  purpose: "用于移动端研发与自动化测试",
  needed_by_date: "2026-09-15",
  currency: "CNY",
  items: [{
    category: "it_equipment",
    name: "研发笔记本",
    specification: "32GB / 1TB",
    quantity: "1.00",
    unit: "台",
    unit_price: "12999.00",
    subtotal: "12999.00",
  }],
  applicant: { display_name: "Alice" },
  organization: { display_name: "研发中心" },
  timeline: [{
    kind: "submitted",
    occurred_at: "2026-08-25T01:00:00Z",
    step_key: null,
    step_label: null,
    action: null,
    actor_display_name: "Alice",
    comment: null,
    status: "submitted",
  }],
};

const pendingProcurement: ProcurementRequestDetail = {
  ...pendingManager,
  id: "10000000-0000-4000-8000-000000000002",
  summary: {
    ...pendingManager.summary,
    request_number: "PR-2026-0002",
    status: "pending_procurement",
  },
  timeline: [
    pendingManager.timeline[0],
    {
      kind: "decision",
      occurred_at: "2026-08-25T02:00:00Z",
      step_key: "department_review",
      step_label: "直属部门负责人审批",
      action: "approve",
      actor_display_name: "部门负责人",
      comment: "同意采购",
      status: "approved",
    },
  ],
};

const rejected: ProcurementRequestDetail = {
  ...pendingManager,
  id: "10000000-0000-4000-8000-000000000003",
  summary: {
    ...pendingManager.summary,
    request_number: "PR-2026-0003",
    status: "rejected",
  },
  timeline: [
    pendingManager.timeline[0],
    {
      kind: "decision",
      occurred_at: "2026-08-25T02:00:00Z",
      step_key: "department_review",
      step_label: "直属部门负责人审批",
      action: "reject",
      actor_display_name: "部门负责人",
      comment: "采购理由不充分",
      status: "rejected",
    },
    {
      kind: "completed",
      occurred_at: "2026-08-25T02:00:00Z",
      step_key: null,
      step_label: null,
      action: null,
      actor_display_name: null,
      comment: null,
      status: "rejected",
    },
  ],
};

const approved: ProcurementRequestDetail = {
  ...pendingManager,
  summary: { ...pendingManager.summary, status: "approved" },
  timeline: [
    pendingManager.timeline[0],
    { ...pendingProcurement.timeline[1], status: "approved", action: "approve" },
    {
      kind: "decision",
      occurred_at: "2026-08-25T03:00:00Z",
      step_key: "procurement_review",
      step_label: "采购专员复核",
      action: "approve",
      actor_display_name: "采购专员",
      comment: "复核通过",
      status: "approved",
    },
    {
      kind: "completed",
      occurred_at: "2026-08-25T03:00:00Z",
      step_key: null,
      step_label: null,
      action: null,
      actor_display_name: null,
      comment: null,
      status: "approved",
    },
  ],
};

const withdrawn: ProcurementRequestDetail = {
  ...pendingProcurement,
  summary: { ...pendingProcurement.summary, status: "cancelled" },
  timeline: [
    pendingManager.timeline[0],
    pendingProcurement.timeline[1],
    {
      kind: "withdrawn",
      occurred_at: "2026-08-25T03:00:00Z",
      step_key: null,
      step_label: null,
      action: null,
      actor_display_name: "Alice",
      comment: null,
      status: "cancelled",
    },
  ],
};

const page: ProcurementRequestPage = {
  items: [
    { id: pendingManager.id, ...pendingManager.summary, submitted_at: "2026-08-25T01:00:00Z" },
    { id: pendingProcurement.id, ...pendingProcurement.summary, submitted_at: "2026-08-25T01:30:00Z" },
    { id: rejected.id, ...rejected.summary, submitted_at: "2026-08-24T01:00:00Z" },
  ],
  offset: 0,
  limit: 20,
  total: 3,
};

const submitted: ProcurementSubmitResult = {
  id: "10000000-0000-4000-8000-000000000004",
  request_number: "PR-2026-0004",
  title: "测试设备",
  total: "20.00",
  status: "pending_manager",
  submitted_at: "2026-08-25T04:00:00Z",
  replayed: false,
};

const conversation: ProcurementConversationSummary = {
  id: "20000000-0000-4000-8000-000000000001",
  title: "采购办事会话",
  created_at: "2026-08-25T01:00:00Z",
  updated_at: "2026-08-25T01:00:00Z",
};

const operationIds = [
  "30000000-0000-4000-8000-000000000001",
  "30000000-0000-4000-8000-000000000002",
  "30000000-0000-4000-8000-000000000003",
  "30000000-0000-4000-8000-000000000004",
];

beforeEach(() => {
  let index = 0;
  vi.mocked(procurementClient.newProcurementOperationId).mockImplementation(
    () => operationIds[index++] ?? crypto.randomUUID(),
  );
  vi.mocked(procurementClient.listProcurementRequests).mockResolvedValue(page);
  vi.mocked(procurementClient.getProcurementRequest).mockImplementation(async id => {
    if (id === pendingProcurement.id) return pendingProcurement;
    if (id === rejected.id) return rejected;
    return pendingManager;
  });
  vi.mocked(procurementClient.previewProcurementRequest).mockResolvedValue({
    currency: "CNY",
    subtotals: ["20.00"],
    total: "20.00",
  });
  vi.mocked(procurementClient.submitProcurementRequest).mockResolvedValue(submitted);
  vi.mocked(procurementClient.withdrawProcurementRequest).mockResolvedValue({
    instance_id: "40000000-0000-4000-8000-000000000001",
    status: "cancelled",
    current_step_key: null,
    replayed: false,
  });
  vi.mocked(procurementClient.createProcurementConversation).mockResolvedValue(conversation);
  vi.mocked(procurementClient.listProcurementConversations).mockResolvedValue([]);
  vi.mocked(procurementClient.sendProcurementTurn).mockResolvedValue({
    id: "50000000-0000-4000-8000-000000000001",
    client_turn_id: operationIds[3],
    role: "assistant",
    text: "已整理你的采购事项。",
    blocks: [{ type: "text", text: "请确认数量、单价和期望日期。" }],
    created_at: "2026-08-25T05:00:00Z",
    replayed: false,
  });
  vi.mocked(procurementClient.confirmProcurementSubmission).mockResolvedValue({
    type: "execution_result",
    resource_type: "procurement_request",
    resource_id: "80000000-0000-4000-8000-000000000001",
    replayed: false,
  });
  vi.mocked(procurementClient.cancelProcurementConfirmation).mockResolvedValue({
    confirmation_id: "70000000-0000-4000-8000-000000000001",
    status: "cancelled",
  });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

async function fillValidForm(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("申请标题"), "测试设备");
  await user.type(screen.getByLabelText("采购用途"), "用于测试采购工作流");
  await user.type(screen.getByLabelText("期望到货日期"), "2026-10-01");
  await user.type(screen.getByLabelText("物品名称 1"), "测试终端");
  await user.type(screen.getByLabelText("数量 1"), "2");
  await user.type(screen.getByLabelText("单位 1"), "台");
    await user.type(screen.getByLabelText("预估单价 1"), "10.00");
  await waitFor(() => expect(procurementClient.previewProcurementRequest).toHaveBeenCalled());
}

function fillValidFormFast(overrides: Partial<Record<
  "title" | "purpose" | "needed_by_date" | "item_name" | "specification" | "quantity" | "unit" | "estimated_unit_price",
  string
>> = {}) {
  fireEvent.change(screen.getByLabelText("申请标题"), { target: { value: overrides.title ?? "测试设备" } });
  fireEvent.change(screen.getByLabelText("采购用途"), { target: { value: overrides.purpose ?? "用于测试采购工作流" } });
  fireEvent.change(screen.getByLabelText("期望到货日期"), { target: { value: overrides.needed_by_date ?? "2026-10-01" } });
  fireEvent.change(screen.getByLabelText("物品名称 1"), { target: { value: overrides.item_name ?? "测试终端" } });
  fireEvent.change(screen.getByLabelText("规格说明 1"), { target: { value: overrides.specification ?? "标准配置" } });
  fireEvent.change(screen.getByLabelText("数量 1"), { target: { value: overrides.quantity ?? "2" } });
  fireEvent.change(screen.getByLabelText("单位 1"), { target: { value: overrides.unit ?? "台" } });
  fireEvent.change(screen.getByLabelText("预估单价 1"), { target: { value: overrides.estimated_unit_price ?? "10.00" } });
}

describe("procurement request workspace", () => {
  it("keeps a deterministic one-to-fifty item form available beside the assistant", async () => {
    const user = userEvent.setup();
    render(<ProcurementPage />);

    expect(screen.getByRole("heading", { name: "新建采购申请" })).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "采购 AI 助手" })).toBeInTheDocument();
    expect(screen.getByText("直属部门负责人审批")).toBeInTheDocument();
    expect(screen.getByText("采购专员复核")).toBeInTheDocument();
    expect(screen.getAllByRole("group", { name: /^采购明细 \d+$/ })).toHaveLength(1);

    const addItem = screen.getByRole("button", { name: "添加采购明细" });
    for (let count = 1; count < 50; count += 1) fireEvent.click(addItem);
    expect(screen.getAllByRole("group", { name: /^采购明细 \d+$/ })).toHaveLength(50);
    expect(screen.getByRole("button", { name: "添加采购明细" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "删除采购明细 50" }));
    expect(screen.getAllByRole("group", { name: /^采购明细 \d+$/ })).toHaveLength(49);
  }, 30_000);

  it("requires a plain-text confirmation and reuses one operation ID for an ambiguous retry", async () => {
    vi.mocked(procurementClient.submitProcurementRequest)
      .mockRejectedValueOnce(new TypeError("network interrupted"))
      .mockResolvedValueOnce(submitted);
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await fillValidForm(user);

    await user.click(screen.getByRole("button", { name: "检查并提交" }));
    const dialog = screen.getByRole("dialog", { name: "提交采购申请确认" });
    expect(within(dialog).getByText("测试设备")).toBeInTheDocument();
    expect(within(dialog).getByText(/直属部门负责人.*采购专员/s)).toBeInTheDocument();
    expect(within(dialog).getByText((_content, element) => (
      element?.tagName === "DD" && element.textContent === "¥20.00"
    ))).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "确认提交" }));
    expect(await screen.findByText(/^请求结果暂时不明确。内容/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "使用同一操作号重试" }));

    await waitFor(() => expect(procurementClient.submitProcurementRequest).toHaveBeenCalledTimes(2));
    const first = vi.mocked(procurementClient.submitProcurementRequest).mock.calls[0];
    const second = vi.mocked(procurementClient.submitProcurementRequest).mock.calls[1];
    expect(first[1]).toBe(second[1]);
    expect(first[0]).toEqual({
      title: "测试设备",
      purpose: "用于测试采购工作流",
      needed_by_date: "2026-10-01",
      currency: "CNY",
      items: [{
        category_code: "office_supplies",
        item_name: "测试终端",
        specification: null,
        quantity: "2",
        unit: "台",
        estimated_unit_price: "10.00",
      }],
    });
    expect(await screen.findByText(/服务器确认总额：¥20.00/)).toBeInTheDocument();
  });

  it.each([408, 429, 500, 502, 503, 504])(
    "keeps the frozen submit intent for ambiguous HTTP %s responses",
    async status => {
      vi.mocked(procurementClient.submitProcurementRequest)
        .mockRejectedValueOnce(new ApiError(status, "request_failed"))
        .mockResolvedValueOnce(submitted);
      const user = userEvent.setup();
      render(<ProcurementPage currentDate="2026-08-25" />);
      fillValidFormFast();
      await screen.findByText("服务器权威总额 ¥20.00");
      await user.click(screen.getByRole("button", { name: "检查并提交" }));
      await user.click(screen.getByRole("button", { name: "确认提交" }));

      const retry = await screen.findByRole("button", { name: "使用同一操作号重试" });
      expect(screen.getByRole("dialog", { name: "提交采购申请确认" })).toBeInTheDocument();
      await user.click(retry);
      await waitFor(() => expect(procurementClient.submitProcurementRequest).toHaveBeenCalledTimes(2));
      const first = vi.mocked(procurementClient.submitProcurementRequest).mock.calls[0];
      const second = vi.mocked(procurementClient.submitProcurementRequest).mock.calls[1];
      expect(second[0]).toEqual(first[0]);
      expect(second[1]).toBe(first[1]);
    },
  );

  it("aborts superseded previews and ignores an older response that resolves last", async () => {
    let resolveFirst: ((value: { currency: "CNY"; subtotals: string[]; total: string }) => void) | undefined;
    vi.mocked(procurementClient.previewProcurementRequest)
      .mockImplementationOnce((_input, signal) => new Promise((resolve, reject) => {
        resolveFirst = resolve;
        signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      }))
      .mockResolvedValueOnce({ currency: "CNY", subtotals: ["30.00"], total: "30.00" });
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await fillValidForm(user);
    const price = screen.getByLabelText("预估单价 1");
    await user.clear(price);
    await user.type(price, "15.00");

    expect(await screen.findByText("服务器权威总额 ¥30.00")).toBeInTheDocument();
    resolveFirst?.({ currency: "CNY", subtotals: ["20.00"], total: "20.00" });
    await Promise.resolve();
    expect(screen.queryByText("服务器权威总额 ¥20.00")).not.toBeInTheDocument();
    const firstSignal = vi.mocked(procurementClient.previewProcurementRequest).mock.calls[0][1];
    expect(firstSignal?.aborted).toBe(true);
  });

  it("never consumes a preview whose canonical input fingerprint is stale", async () => {
    let resolveNext: ((value: { currency: "CNY"; subtotals: string[]; total: string }) => void) | undefined;
    vi.mocked(procurementClient.previewProcurementRequest)
      .mockResolvedValueOnce({ currency: "CNY", subtotals: ["20.00"], total: "20.00" })
      .mockImplementationOnce(() => new Promise(resolve => { resolveNext = resolve; }));
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await fillValidForm(user);
    expect(await screen.findByText("服务器权威总额 ¥20.00")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查并提交" })).toBeEnabled();

    const price = screen.getByLabelText("预估单价 1");
    await user.clear(price);
    await user.type(price, "15.00");
    expect(screen.queryByText("服务器权威总额 ¥20.00")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查并提交" })).toBeDisabled();
    await waitFor(() => expect(procurementClient.previewProcurementRequest).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "检查并提交" })).toBeDisabled();

    resolveNext?.({ currency: "CNY", subtotals: ["30.00"], total: "30.00" });
    expect(await screen.findByText("服务器权威总额 ¥30.00")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查并提交" })).toBeEnabled();

    await user.clear(screen.getByLabelText("单位 1"));
    expect(screen.queryByText("服务器权威总额 ¥30.00")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查并提交" })).toBeDisabled();
  });

  it.each([
    ["采购用途", "purpose", "用".repeat(2000), "用".repeat(2001), 2000],
    ["物品名称 1", "item_name", "名".repeat(200), "名".repeat(201), 200],
    ["规格说明 1", "specification", "规".repeat(500), "规".repeat(501), 500],
    ["单位 1", "unit", "单".repeat(40), "单".repeat(41), 40],
  ] as const)("accepts the %s backend limit and rejects one character over it", async (label, field, boundary, over, maxLength) => {
    render(<ProcurementPage currentDate="2026-08-25" />);
    fillValidFormFast({ [field]: boundary });
    await waitFor(() => expect(procurementClient.previewProcurementRequest).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText(label)).toHaveAttribute("maxlength", String(maxLength));

    vi.mocked(procurementClient.previewProcurementRequest).mockClear();
    fireEvent.change(screen.getByLabelText(label), { target: { value: over } });
    await new Promise(resolve => window.setTimeout(resolve, 220));
    expect(procurementClient.previewProcurementRequest).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "检查并提交" })).toBeDisabled();
  });

  it("accepts today as the needed date and rejects past or impossible ISO dates", async () => {
    render(<ProcurementPage currentDate="2026-08-25" />);
    fillValidFormFast({ needed_by_date: "2026-08-25" });
    await waitFor(() => expect(procurementClient.previewProcurementRequest).toHaveBeenCalledTimes(1));

    vi.mocked(procurementClient.previewProcurementRequest).mockClear();
    fireEvent.change(screen.getByLabelText("期望到货日期"), { target: { value: "2026-08-24" } });
    await new Promise(resolve => window.setTimeout(resolve, 220));
    expect(procurementClient.previewProcurementRequest).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("期望到货日期"), { target: { value: "2026-02-30" } });
    await new Promise(resolve => window.setTimeout(resolve, 220));
    expect(procurementClient.previewProcurementRequest).not.toHaveBeenCalled();
  });

  it("does not request a server preview until every required field is locally valid", async () => {
    render(<ProcurementPage currentDate="2026-08-25" />);
    fireEvent.change(screen.getByLabelText("申请标题"), { target: { value: "不完整申请" } });
    fireEvent.change(screen.getByLabelText("采购用途"), { target: { value: "只填写了主信息" } });
    await new Promise(resolve => window.setTimeout(resolve, 220));
    expect(procurementClient.previewProcurementRequest).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("期望到货日期"), { target: { value: "2026-10-01" } });
    fireEvent.change(screen.getByLabelText("物品名称 1"), { target: { value: "键盘" } });
    fireEvent.change(screen.getByLabelText("数量 1"), { target: { value: "0" } });
    fireEvent.change(screen.getByLabelText("单位 1"), { target: { value: "个" } });
    fireEvent.change(screen.getByLabelText("预估单价 1"), { target: { value: "12.00" } });
    await new Promise(resolve => window.setTimeout(resolve, 220));
    expect(procurementClient.previewProcurementRequest).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("数量 1"), { target: { value: "1" } });
    await waitFor(() => expect(procurementClient.previewProcurementRequest).toHaveBeenCalledTimes(1));
  });

  it("discards an abandoned submit intent and allocates a new operation ID next time", async () => {
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    fillValidFormFast();
    await waitFor(() => expect(procurementClient.previewProcurementRequest).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "检查并提交" }));
    await user.click(screen.getByRole("button", { name: "返回修改" }));
    await user.click(screen.getByRole("button", { name: "检查并提交" }));
    expect(procurementClient.newProcurementOperationId).toHaveBeenCalledTimes(2);
  });

  it("allows withdrawal in both running stages but never for terminal requests", async () => {
    const user = userEvent.setup();
    render(<ProcurementPage />);
    await screen.findByRole("button", { name: "查看 PR-2026-0001" });

    await user.click(screen.getByRole("button", { name: "查看 PR-2026-0001" }));
    expect(await screen.findByRole("button", { name: "撤回 PR-2026-0001" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "查看 PR-2026-0002" }));
    expect(await screen.findByRole("button", { name: "撤回 PR-2026-0002" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "查看 PR-2026-0003" }));
    expect(screen.queryByRole("button", { name: "撤回 PR-2026-0003" })).not.toBeInTheDocument();
  });

  it("passes status and submitted-date filters and labels overview counts as current-page facts", async () => {
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await screen.findByRole("button", { name: "查看 PR-2026-0001" });
    expect(screen.getByText("当前页状态分布")).toBeInTheDocument();
    expect(screen.getByText(/待负责人 1.*待采购 1.*已拒绝 1/)).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("申请状态"), "rejected");
    await user.type(screen.getByLabelText("提交日期从"), "2026-08-01");
    await user.type(screen.getByLabelText("提交日期至"), "2026-08-25");
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => expect(procurementClient.listProcurementRequests).toHaveBeenCalledTimes(2));
    expect(procurementClient.listProcurementRequests).toHaveBeenLastCalledWith(expect.objectContaining({
      status: "rejected",
      submittedFrom: "2026-08-01",
      submittedTo: "2026-08-25",
      offset: 0,
      limit: 20,
      signal: expect.any(AbortSignal),
    }));
  });

  it("rejects an inverted submitted-date range locally without issuing a request", async () => {
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await screen.findByRole("button", { name: "查看 PR-2026-0001" });
    await user.type(screen.getByLabelText("提交日期从"), "2026-08-25");
    await user.type(screen.getByLabelText("提交日期至"), "2026-08-01");
    await user.click(screen.getByRole("button", { name: "应用筛选" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("开始日期不能晚于结束日期");
    expect(procurementClient.listProcurementRequests).toHaveBeenCalledTimes(1);
    await user.clear(screen.getByLabelText("提交日期至"));
    await user.type(screen.getByLabelText("提交日期至"), "2026-08-25");
    expect(screen.queryByText("开始日期不能晚于结束日期")).not.toBeInTheDocument();
  });

  it("exposes total-aware pagination and sends bounded offsets", async () => {
    vi.mocked(procurementClient.listProcurementRequests)
      .mockResolvedValueOnce({ ...page, total: 42 })
      .mockResolvedValueOnce({ ...page, items: [page.items[2]], offset: 20, total: 42 })
      .mockResolvedValueOnce({ ...page, total: 42 });
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);

    expect(await screen.findByText(/共 42 条/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(procurementClient.listProcurementRequests).toHaveBeenCalledTimes(2));
    expect(procurementClient.listProcurementRequests).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 20, limit: 20 }));
    expect(screen.getByRole("button", { name: "上一页" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "上一页" }));
    await waitFor(() => expect(procurementClient.listProcurementRequests).toHaveBeenCalledTimes(3));
    expect(procurementClient.listProcurementRequests).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 0, limit: 20 }));
  });

  it("aborts superseded list reads and ignores reverse-order results", async () => {
    let resolveFirst: ((value: ProcurementRequestPage) => void) | undefined;
    let resolveSecond: ((value: ProcurementRequestPage) => void) | undefined;
    vi.mocked(procurementClient.listProcurementRequests)
      .mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve; }))
      .mockImplementationOnce(() => new Promise(resolve => { resolveSecond = resolve; }));
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await waitFor(() => expect(procurementClient.listProcurementRequests).toHaveBeenCalledTimes(1));
    await user.selectOptions(screen.getByLabelText("申请状态"), "rejected");
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => expect(procurementClient.listProcurementRequests).toHaveBeenCalledTimes(2));

    const firstSignal = vi.mocked(procurementClient.listProcurementRequests).mock.calls[0]?.[0]?.signal;
    expect(firstSignal?.aborted).toBe(true);
    resolveSecond?.({ ...page, items: [page.items[2]], total: 1 });
    expect(await screen.findByRole("button", { name: "查看 PR-2026-0003" })).toBeInTheDocument();
    resolveFirst?.(page);
    await Promise.resolve();
    expect(screen.queryByRole("button", { name: "查看 PR-2026-0001" })).not.toBeInTheDocument();
  });

  it("aborts superseded detail reads and ignores reverse-order results", async () => {
    let resolveFirst: ((value: ProcurementRequestDetail) => void) | undefined;
    vi.mocked(procurementClient.getProcurementRequest)
      .mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve; }))
      .mockResolvedValueOnce(pendingProcurement);
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await user.click(await screen.findByRole("button", { name: "查看 PR-2026-0001" }));
    await user.click(screen.getByRole("button", { name: "查看 PR-2026-0002" }));

    expect(await screen.findByRole("dialog", { name: "采购申请 PR-2026-0002" })).toBeInTheDocument();
    const firstSignal = vi.mocked(procurementClient.getProcurementRequest).mock.calls[0][1];
    expect(firstSignal?.aborted).toBe(true);
    resolveFirst?.(pendingManager);
    await Promise.resolve();
    expect(screen.queryByRole("dialog", { name: "采购申请 PR-2026-0001" })).not.toBeInTheDocument();
  });

  it("aborts outstanding list and detail reads when unmounted", async () => {
    vi.mocked(procurementClient.listProcurementRequests).mockImplementationOnce(() => new Promise(() => {}));
    const firstRender = render(<ProcurementPage currentDate="2026-08-25" />);
    await waitFor(() => expect(procurementClient.listProcurementRequests).toHaveBeenCalledTimes(1));
    const listSignal = vi.mocked(procurementClient.listProcurementRequests).mock.calls[0]?.[0]?.signal;
    firstRender.unmount();
    expect(listSignal?.aborted).toBe(true);

    vi.mocked(procurementClient.listProcurementRequests).mockResolvedValue(page);
    vi.mocked(procurementClient.getProcurementRequest).mockImplementationOnce(() => new Promise(() => {}));
    const secondRender = render(<ProcurementPage currentDate="2026-08-25" />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "查看 PR-2026-0001" }));
    const detailSignal = vi.mocked(procurementClient.getProcurementRequest).mock.calls[0][1];
    secondRender.unmount();
    expect(detailSignal?.aborted).toBe(true);
  });

  it("treats detail and confirmation overlays as focus-contained accessible dialogs", async () => {
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    const detailTrigger = await screen.findByRole("button", { name: "查看 PR-2026-0001" });
    await user.click(detailTrigger);
    const detailDialog = await screen.findByRole("dialog", { name: "采购申请 PR-2026-0001" });
    const detailClose = within(detailDialog).getByRole("button", { name: "关闭采购详情" });
    const detailLast = within(detailDialog).getByRole("button", { name: "复制 PR-2026-0001 新建" });
    expect(detailClose).toHaveFocus();
    detailLast.focus();
    await user.tab();
    expect(detailClose).toHaveFocus();
    await user.tab({ shift: true });
    expect(detailLast).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "采购申请 PR-2026-0001" })).not.toBeInTheDocument();
    expect(detailTrigger).toHaveFocus();

    fillValidFormFast();
    await screen.findByText("服务器权威总额 ¥20.00");
    const submitTrigger = screen.getByRole("button", { name: "检查并提交" });
    await user.click(submitTrigger);
    const confirmationDialog = screen.getByRole("dialog", { name: "提交采购申请确认" });
    const cancel = within(confirmationDialog).getByRole("button", { name: "返回修改" });
    const confirm = within(confirmationDialog).getByRole("button", { name: "确认提交" });
    expect(cancel).toHaveFocus();
    confirm.focus();
    await user.tab();
    expect(cancel).toHaveFocus();
    await user.tab({ shift: true });
    expect(confirm).toHaveFocus();
    expect(within(confirmationDialog).getByText("提交后由服务器生成")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "提交采购申请确认" })).not.toBeInTheDocument();
    expect(submitTrigger).toHaveFocus();
  }, 10_000);

  it("makes the underlying detail inert while withdrawal confirmation is active", async () => {
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await user.click(await screen.findByRole("button", { name: "查看 PR-2026-0002" }));
    const detail = await screen.findByRole("dialog", { name: "采购申请 PR-2026-0002" });
    const detailOverlay = detail.parentElement;
    await user.click(within(detail).getByRole("button", { name: "撤回 PR-2026-0002" }));

    expect(screen.getByRole("dialog", { name: "撤回采购申请确认" })).toHaveAttribute("aria-modal", "true");
    expect(detail).not.toHaveAttribute("aria-modal", "true");
    expect(detailOverlay).toHaveAttribute("aria-hidden", "true");
    expect(detailOverlay).toHaveAttribute("inert");
  });

  it("confirms withdrawal and refreshes authoritative state after a 409 conflict", async () => {
    vi.mocked(procurementClient.withdrawProcurementRequest).mockRejectedValueOnce(
      new ApiError(409, "procurement_request_state_conflict", { detail: "secret server state" }),
    );
    const user = userEvent.setup();
    render(<ProcurementPage />);
    await user.click(await screen.findByRole("button", { name: "查看 PR-2026-0002" }));
    await user.click(await screen.findByRole("button", { name: "撤回 PR-2026-0002" }));
    await user.click(screen.getByRole("button", { name: "确认撤回" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("状态已变化，已刷新服务器中的真实状态");
    expect(screen.queryByText("secret server state")).not.toBeInTheDocument();
    expect(procurementClient.listProcurementRequests).toHaveBeenCalledTimes(2);
    expect(procurementClient.getProcurementRequest).toHaveBeenCalledTimes(2);
  });

  it("reuses the withdrawal operation ID when the response is ambiguous", async () => {
    vi.mocked(procurementClient.withdrawProcurementRequest)
      .mockRejectedValueOnce(new TypeError("network interrupted"))
      .mockResolvedValueOnce({
        instance_id: "40000000-0000-4000-8000-000000000001",
        status: "cancelled",
        current_step_key: null,
        replayed: false,
      });
    const user = userEvent.setup();
    render(<ProcurementPage />);
    await user.click(await screen.findByRole("button", { name: "查看 PR-2026-0002" }));
    await user.click(await screen.findByRole("button", { name: "撤回 PR-2026-0002" }));
    await user.click(screen.getByRole("button", { name: "确认撤回" }));
    await user.click(await screen.findByRole("button", { name: "使用同一操作号重试" }));
    await waitFor(() => expect(procurementClient.withdrawProcurementRequest).toHaveBeenCalledTimes(2));
    expect(vi.mocked(procurementClient.withdrawProcurementRequest).mock.calls[0][1])
      .toBe(vi.mocked(procurementClient.withdrawProcurementRequest).mock.calls[1][1]);
  });

  it("keeps the frozen withdrawal target and operation ID after an ambiguous service response", async () => {
    let resolveOtherDetail: ((value: ProcurementRequestDetail) => void) | undefined;
    vi.mocked(procurementClient.getProcurementRequest)
      .mockResolvedValueOnce(pendingProcurement)
      .mockImplementationOnce(() => new Promise(resolve => { resolveOtherDetail = resolve; }));
    vi.mocked(procurementClient.withdrawProcurementRequest)
      .mockRejectedValueOnce(new ApiError(503, "request_failed"))
      .mockResolvedValueOnce({ instance_id: "40000000-0000-4000-8000-000000000001", status: "cancelled", current_step_key: null, replayed: false });
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    const otherTrigger = await screen.findByRole("button", { name: "查看 PR-2026-0001" });
    await user.click(screen.getByRole("button", { name: "查看 PR-2026-0002" }));
    await user.click(await screen.findByRole("button", { name: "撤回 PR-2026-0002" }));
    fireEvent.click(otherTrigger);
    resolveOtherDetail?.(pendingManager);
    await Promise.resolve();
    await user.click(screen.getByRole("button", { name: "确认撤回" }));
    await user.click(await screen.findByRole("button", { name: "使用同一操作号重试" }));

    await waitFor(() => expect(procurementClient.withdrawProcurementRequest).toHaveBeenCalledTimes(2));
    const first = vi.mocked(procurementClient.withdrawProcurementRequest).mock.calls[0];
    const second = vi.mocked(procurementClient.withdrawProcurementRequest).mock.calls[1];
    expect(first[0]).toBe(pendingProcurement.id);
    expect(second[0]).toBe(first[0]);
    expect(second[1]).toBe(first[1]);
  });

  it("copies only allowed business fields and starts a new submit intent", async () => {
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await user.click(await screen.findByRole("button", { name: "查看 PR-2026-0003" }));
    await user.click(await screen.findByRole("button", { name: "复制 PR-2026-0003 新建" }));

    expect(screen.getByLabelText("申请标题")).toHaveValue(rejected.summary.title);
    expect(screen.getByLabelText("采购用途")).toHaveValue(rejected.purpose);
    expect(screen.queryByDisplayValue(rejected.summary.request_number)).not.toBeInTheDocument();
    await waitFor(() => expect(procurementClient.previewProcurementRequest).toHaveBeenCalled());
    await screen.findByText("服务器权威总额 ¥20.00");
    await user.click(screen.getByRole("button", { name: "检查并提交" }));
    await user.click(screen.getByRole("button", { name: "确认提交" }));

    await waitFor(() => expect(procurementClient.submitProcurementRequest).toHaveBeenCalledTimes(1));
    const [payload, operationId] = vi.mocked(procurementClient.submitProcurementRequest).mock.calls[0];
    expect(payload).not.toHaveProperty("id");
    expect(payload).not.toHaveProperty("request_number");
    expect(payload).not.toHaveProperty("approval_instance_id");
    expect(operationId).toMatch(/^30000000-/);
  });

  it("holds one client turn ID across an ambiguous assistant retry and creates another for new text", async () => {
    vi.mocked(procurementClient.sendProcurementTurn)
      .mockRejectedValueOnce(new TypeError("network interrupted"))
      .mockResolvedValueOnce({
        id: "50000000-0000-4000-8000-000000000001",
        client_turn_id: operationIds[0],
        role: "assistant",
        text: "已整理你的采购事项。",
        blocks: [{ type: "text", text: "请确认数量、单价和期望日期。" }],
        created_at: "2026-08-25T05:00:00Z",
        replayed: false,
      })
      .mockResolvedValueOnce({
        id: "50000000-0000-4000-8000-000000000002",
        client_turn_id: operationIds[1],
        role: "assistant",
        text: "这是新的事项。",
        blocks: [{ type: "text", text: "收到。" }],
        created_at: "2026-08-25T05:01:00Z",
        replayed: false,
      });
    const user = userEvent.setup();
    render(<ProcurementPage />);

    await user.type(screen.getByLabelText("向采购 AI 助手说明事项"), "帮我梳理电脑采购信息");
    await user.click(screen.getByRole("button", { name: "发送给采购 AI 助手" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("内容已保留，可以安全重试");
    await user.click(screen.getByRole("button", { name: "重试采购 AI 请求" }));
    await waitFor(() => expect(procurementClient.sendProcurementTurn).toHaveBeenCalledTimes(2));
    expect(vi.mocked(procurementClient.sendProcurementTurn).mock.calls[0][2])
      .toBe(vi.mocked(procurementClient.sendProcurementTurn).mock.calls[1][2]);

    await user.clear(screen.getByLabelText("向采购 AI 助手说明事项"));
    await user.type(screen.getByLabelText("向采购 AI 助手说明事项"), "查询我的采购申请");
    await user.click(screen.getByRole("button", { name: "发送给采购 AI 助手" }));
    await waitFor(() => expect(procurementClient.sendProcurementTurn).toHaveBeenCalledTimes(3));
    expect(vi.mocked(procurementClient.sendProcurementTurn).mock.calls[2][2])
      .not.toBe(vi.mocked(procurementClient.sendProcurementTurn).mock.calls[1][2]);
  });

  it("sends the procurement assistant on Enter, keeps Shift+Enter, ignores IME, and shows the shared hint", async () => {
    const user = userEvent.setup();
    render(<ProcurementPage />);
    const composer = screen.getByLabelText("向采购 AI 助手说明事项");

    await user.type(composer, "第一行{shift>}{enter}{/shift}第二行");
    expect(composer).toHaveValue("第一行\n第二行");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(procurementClient.sendProcurementTurn).toHaveBeenCalledTimes(1));

    await user.type(composer, "拼音");
    fireEvent.keyDown(composer, { key: "Enter", code: "Enter", isComposing: true });
    expect(procurementClient.sendProcurementTurn).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Enter 发送 · Shift+Enter 换行")).toBeInTheDocument();
  });

  it("prevents two synchronous assistant submits while leaving ordinary procurement textareas untouched", async () => {
    vi.mocked(procurementClient.sendProcurementTurn).mockReturnValue(new Promise(() => {}));
    render(<ProcurementPage />);
    const assistant = screen.getByLabelText("向采购 AI 助手说明事项");
    fireEvent.change(assistant, { target: { value: "同一事件周期只发送一次" } });
    const form = assistant.closest("form");
    expect(form).not.toBeNull();

    fireEvent.keyDown(screen.getByLabelText("采购用途"), { key: "Enter", code: "Enter" });
    expect(procurementClient.sendProcurementTurn).not.toHaveBeenCalled();
    act(() => {
      form!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      form!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    await waitFor(() => expect(procurementClient.sendProcurementTurn).toHaveBeenCalledTimes(1));
  });

  it("renders and confirms an AI procurement proposal through the existing confirmation endpoint", async () => {
    vi.mocked(procurementClient.sendProcurementTurn).mockResolvedValueOnce({
      id: "50000000-0000-4000-8000-000000000003",
      client_turn_id: operationIds[0],
      role: "assistant",
      text: "请核对采购申请并明确确认。",
      blocks: [{
        type: "confirmation",
        confirmation_id: "70000000-0000-4000-8000-000000000001",
        tool_name: "procurement.submit_request",
        preview: {
          type: "procurement_request",
          title: "研发电脑",
          purpose: "用于移动端研发",
          needed_by_date: "2026-09-15",
          currency: "CNY",
          item_count: 1,
          total: "12999.00",
          actor_user_id: "must-not-render",
        },
        expires_at: "2030-08-26T13:00:00Z",
      }],
      created_at: "2026-08-26T12:00:00Z",
      replayed: false,
    });
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-26" />);

    await user.type(screen.getByLabelText("向采购 AI 助手说明事项"), "帮我提交研发电脑采购申请");
    await user.click(screen.getByRole("button", { name: "发送给采购 AI 助手" }));

    const card = await screen.findByRole("group", { name: "采购 AI 提交确认" });
    expect(within(card).getByText("研发电脑")).toBeInTheDocument();
    expect(within(card).queryByText("must-not-render")).not.toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "确认提交 AI 采购申请" }));

    await waitFor(() => expect(procurementClient.confirmProcurementSubmission).toHaveBeenCalledWith(
      "70000000-0000-4000-8000-000000000001",
      operationIds[1],
    ));
    expect(within(card).getByRole("status")).toHaveTextContent("采购申请已创建");
    await waitFor(() => expect(procurementClient.listProcurementRequests).toHaveBeenCalledTimes(2));
  });

  it("treats request_failed assistant responses as ambiguous and reuses the turn ID", async () => {
    vi.mocked(procurementClient.sendProcurementTurn)
      .mockRejectedValueOnce(new ApiError(500, "request_failed"))
      .mockResolvedValueOnce({ id: "50000000-0000-4000-8000-000000000002", client_turn_id: operationIds[0], role: "assistant", text: "已恢复。", blocks: [{ type: "text", text: "已恢复。" }], created_at: "2026-08-25T05:01:00Z", replayed: false });
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await user.type(screen.getByLabelText("向采购 AI 助手说明事项"), "帮我梳理采购信息");
    await user.click(screen.getByRole("button", { name: "发送给采购 AI 助手" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("内容已保留，可以安全重试");
    await user.click(screen.getByRole("button", { name: "重试采购 AI 请求" }));
    await waitFor(() => expect(procurementClient.sendProcurementTurn).toHaveBeenCalledTimes(2));
    expect(vi.mocked(procurementClient.sendProcurementTurn).mock.calls[1][2])
      .toBe(vi.mocked(procurementClient.sendProcurementTurn).mock.calls[0][2]);
  });

  it("renders production-shaped submitted and decision entries without inventing pending decisions", async () => {
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await user.click(await screen.findByRole("button", { name: "查看 PR-2026-0002" }));
    const timeline = await screen.findByRole("list", { name: "审批时间线" });
    expect(within(timeline).getByText("申请已提交")).toBeInTheDocument();
    expect(within(timeline).getByText("已提交")).toBeInTheDocument();
    expect(within(timeline).getByText("直属部门负责人审批")).toBeInTheDocument();
    expect(within(timeline).getByText("已批准")).toBeInTheDocument();
    expect(timeline.querySelectorAll("svg").length).toBeGreaterThanOrEqual(2);
    expect(within(timeline).queryByText(/\b(?:submitted|approved|pending)\b/i)).not.toBeInTheDocument();
  });

  it("renders approved, rejected, and withdrawn terminal entries with exact Chinese outcomes", async () => {
    vi.mocked(procurementClient.getProcurementRequest)
      .mockResolvedValueOnce(approved)
      .mockResolvedValueOnce(rejected)
      .mockResolvedValueOnce(withdrawn);
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);

    await user.click(await screen.findByRole("button", { name: "查看 PR-2026-0001" }));
    let timeline = await screen.findByRole("list", { name: "审批时间线" });
    expect(within(timeline).getByText("申请已完成")).toBeInTheDocument();
    expect(within(timeline).getByText("已完成")).toBeInTheDocument();
    expect(within(timeline).queryByText("申请已提交", { selector: "strong" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "关闭采购详情" }));

    await user.click(screen.getByRole("button", { name: "查看 PR-2026-0003" }));
    timeline = await screen.findByRole("list", { name: "审批时间线" });
    expect(within(timeline).getByText("申请已拒绝并终止")).toBeInTheDocument();
    expect(within(timeline).getAllByText("已拒绝")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "关闭采购详情" }));

    await user.click(screen.getByRole("button", { name: "查看 PR-2026-0002" }));
    timeline = await screen.findByRole("list", { name: "审批时间线" });
    expect(within(timeline).getByText("申请已撤回")).toBeInTheDocument();
    expect(within(timeline).getByText("已撤回")).toBeInTheDocument();
    expect(within(timeline).queryByText("已取消")).not.toBeInTheDocument();
  });

  it("maps stable error codes without rendering server detail text", async () => {
    vi.mocked(procurementClient.submitProcurementRequest).mockRejectedValueOnce(
      new ApiError(422, "procurement_manager_unavailable", { detail: "manager email and raw SQL" }),
    );
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    fillValidFormFast();
    await screen.findByText("服务器权威总额 ¥20.00");
    await user.click(screen.getByRole("button", { name: "检查并提交" }));
    await user.click(screen.getByRole("button", { name: "确认提交" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("直属部门负责人暂不可用");
    expect(screen.queryByText(/manager email|raw SQL/)).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "提交采购申请确认" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查并提交" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "检查并提交" }));
    expect(procurementClient.newProcurementOperationId).toHaveBeenCalledTimes(2);
  });

  it("closes withdrawal confirmation after a recognized non-conflict error", async () => {
    vi.mocked(procurementClient.withdrawProcurementRequest).mockRejectedValueOnce(
      new ApiError(422, "procurement_request_state_conflict", { detail: "raw workflow state" }),
    );
    const user = userEvent.setup();
    render(<ProcurementPage currentDate="2026-08-25" />);
    await user.click(await screen.findByRole("button", { name: "查看 PR-2026-0002" }));
    await user.click(await screen.findByRole("button", { name: "撤回 PR-2026-0002" }));
    await user.click(screen.getByRole("button", { name: "确认撤回" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("申请状态已发生变化");
    expect(screen.queryByRole("dialog", { name: "撤回采购申请确认" })).not.toBeInTheDocument();
    expect(screen.queryByText("raw workflow state")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "撤回 PR-2026-0002" })).toBeEnabled();
  });
});
