import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/request";
import * as hrClient from "./client";
import { HrAssistantPage } from "./HrAssistantPage";
import type { HrConversationDetail, HrTurnResponse } from "./types";

vi.mock("./client", () => ({
  listConversations: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  newClientId: vi.fn(() => crypto.randomUUID()),
  sendTurn: vi.fn(),
  archiveConversation: vi.fn(),
  confirmTool: vi.fn(),
  cancelTool: vi.fn(),
}));

const conversation = {
  id: "10000000-0000-4000-8000-000000000001",
  title: "请假咨询",
  created_at: "2026-08-16T08:00:00Z",
  updated_at: "2026-08-16T08:01:00Z",
};

const emptyDetail: HrConversationDetail = { ...conversation, turns: [] };

function response(blocks: HrTurnResponse["blocks"], text = "测试事项"): HrTurnResponse {
  return {
    client_turn_id: crypto.randomUUID(),
    text,
    blocks,
    model_calls: 1,
    read_calls: 0,
    write_proposals: 0,
  };
}

beforeEach(() => {
  vi.mocked(hrClient.listConversations).mockResolvedValue([conversation]);
  vi.mocked(hrClient.getConversation).mockResolvedValue(emptyDetail);
  vi.mocked(hrClient.createConversation).mockResolvedValue(conversation);
  vi.mocked(hrClient.sendTurn).mockResolvedValue(response([{ type: "text", text: "已处理" }]));
  vi.mocked(hrClient.archiveConversation).mockResolvedValue();
  vi.mocked(hrClient.cancelTool).mockResolvedValue({ confirmation_id: "c1", status: "cancelled" });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("structured HR assistant", () => {
  it("loads conversation history, opens a conversation, and creates a new case", async () => {
    const user = userEvent.setup();
    render(<HrAssistantPage />);

    expect(screen.getByRole("heading", { level: 1, name: "AI 请假助手" })).toBeInTheDocument();
    expect(screen.getByText("查询假期余额、了解请假制度、提交与撤销请假申请")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "请假咨询" })).toBeInTheDocument();
    expect(hrClient.getConversation).toHaveBeenCalledWith(conversation.id);
    await user.click(screen.getByRole("button", { name: "新建办事" }));

    expect(hrClient.createConversation).toHaveBeenCalledWith();
    expect(await screen.findByText("还没有办事记录")).toBeInTheDocument();
    expect(screen.getByText("请直接描述你要办理或查询的事项。")).toBeInTheDocument();
  });

  it("archives only the selected owned conversation and opens the next retained conversation", async () => {
    const retained = {
      ...conversation,
      id: "10000000-0000-4000-8000-000000000002",
      title: "保留的会话",
      updated_at: "2026-08-16T07:59:00Z",
    };
    vi.mocked(hrClient.listConversations).mockResolvedValue([conversation, retained]);
    vi.mocked(hrClient.getConversation).mockImplementation(async id => (
      id === conversation.id ? emptyDetail : { ...retained, turns: [] }
    ));
    let releaseArchive!: () => void;
    vi.mocked(hrClient.archiveConversation).mockReturnValue(
      new Promise<void>(resolve => { releaseArchive = resolve; }),
    );
    const user = userEvent.setup();
    render(<HrAssistantPage />);

    await screen.findByRole("button", { name: "请假咨询" });
    const remove = screen.getByRole("button", { name: "删除 请假咨询" });
    await user.click(remove);
    expect(remove).toBeDisabled();
    expect(hrClient.archiveConversation).toHaveBeenCalledWith(conversation.id);

    await act(async () => releaseArchive());
    await waitFor(() => expect(screen.queryByRole("button", { name: "请假咨询" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "保留的会话" })).toBeInTheDocument();
    await waitFor(() => expect(hrClient.getConversation).toHaveBeenCalledWith(retained.id));
  });

  it("retains a conversation and shows an inline error when archive fails", async () => {
    vi.mocked(hrClient.archiveConversation).mockRejectedValue(new Error("network unavailable"));
    const user = userEvent.setup();
    render(<HrAssistantPage />);

    await screen.findByRole("button", { name: "请假咨询" });
    await user.click(screen.getByRole("button", { name: "删除 请假咨询" }));

    expect(await screen.findByRole("alert", { name: "删除办事记录失败" }))
      .toHaveTextContent("暂时无法删除这条办事记录，请稍后重试。");
    expect(screen.getByRole("button", { name: "请假咨询" })).toBeInTheDocument();
  });

  it("keeps a newly created case when the initial history request finishes late", async () => {
    const newConversation = {
      ...conversation,
      id: "10000000-0000-4000-8000-000000000099",
      title: "新办事",
      updated_at: "2026-08-16T08:02:00Z",
    };
    let releaseHistory!: (value: HrConversationDetail) => void;
    vi.mocked(hrClient.getConversation).mockReturnValue(
      new Promise((resolve) => { releaseHistory = resolve; }),
    );
    vi.mocked(hrClient.createConversation).mockResolvedValue(newConversation);
    const user = userEvent.setup();
    render(<HrAssistantPage />);

    expect(await screen.findByRole("button", { name: "请假咨询" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "新建办事" }));
    expect(await screen.findByText("还没有办事记录")).toBeInTheDocument();

    await act(async () => {
      releaseHistory({
        ...emptyDetail,
        turns: [{
          client_turn_id: "20000000-0000-4000-8000-000000000099",
          text: "不应覆盖新会话的旧事项",
          created_at: "2026-08-16T08:01:00Z",
          blocks: [{ type: "text", text: "旧请求完成" }],
          model_calls: 1,
          read_calls: 0,
          write_proposals: 0,
        }],
      });
    });

    expect(screen.getByText("还没有办事记录")).toBeInTheDocument();
    expect(screen.queryByText("不应覆盖新会话的旧事项")).not.toBeInTheDocument();
  });

  it("submits with Enter, keeps Shift+Enter as a newline, and ignores IME composition", async () => {
    const user = userEvent.setup();
    render(<HrAssistantPage />);
    const composer = await screen.findByRole("textbox", { name: "向 AI 请假助手说明事项" });

    await user.type(composer, "第一行{shift>}{enter}{/shift}第二行");
    expect(composer).toHaveValue("第一行\n第二行");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(hrClient.sendTurn).toHaveBeenCalledWith(conversation.id, "第一行\n第二行", expect.any(String)));

    await user.type(composer, "拼音");
    composer.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, isComposing: true }));
    expect(hrClient.sendTurn).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Enter 发送 · Shift\+Enter 换行/)).toBeInTheDocument();
  });

  it("reuses one client turn id after an ambiguous failure and clears it after deterministic outcomes", async () => {
    vi.mocked(hrClient.sendTurn)
      .mockRejectedValueOnce(new TypeError("network interrupted"))
      .mockResolvedValueOnce(response([{ type: "text", text: "已恢复" }], "查询余额"))
      .mockRejectedValueOnce({ status: 422, code: "request_validation_failed" })
      .mockResolvedValueOnce(response([{ type: "text", text: "新请求已处理" }], "确定性错误"));
    const user = userEvent.setup();
    render(<HrAssistantPage />);
    const composer = await screen.findByRole("textbox", { name: "向 AI 请假助手说明事项" });

    await user.type(composer, "查询余额");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("本次请求未完成。内容已保留，可以安全重试。");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(hrClient.sendTurn).toHaveBeenCalledTimes(2));
    expect(vi.mocked(hrClient.sendTurn).mock.calls[1][2])
      .toBe(vi.mocked(hrClient.sendTurn).mock.calls[0][2]);

    await user.type(composer, "确定性错误");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("本次请求未完成。内容已保留，可以安全重试。");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(hrClient.sendTurn).toHaveBeenCalledTimes(4));
    expect(vi.mocked(hrClient.sendTurn).mock.calls[3][2])
      .not.toBe(vi.mocked(hrClient.sendTurn).mock.calls[2][2]);
  });

  it("prevents two synchronous submit events from starting duplicate HR turns", async () => {
    vi.mocked(hrClient.sendTurn).mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    render(<HrAssistantPage />);
    const composer = await screen.findByRole("textbox", { name: "向 AI 请假助手说明事项" });
    await user.type(composer, "同一事件周期只发送一次");
    const form = composer.closest("form");
    expect(form).not.toBeNull();

    act(() => {
      form!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      form!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    await waitFor(() => expect(hrClient.sendTurn).toHaveBeenCalledTimes(1));
  });

  it("keeps HR business facts distinct from policy evidence and supports clarification chips", async () => {
    vi.mocked(hrClient.getConversation).mockResolvedValue({
      ...conversation,
      turns: [{
        client_turn_id: "20000000-0000-4000-8000-000000000001",
        text: "查询我的年假余额",
        created_at: "2026-08-16T08:02:00Z",
        blocks: [
          { type: "business_facts", queried_at: "2026-08-16T08:02:00Z", facts: [{ label: "leave_balances", value: [{ leave_type_code: "annual", leave_type_name: "年假", year: 2026, entitled: "10.00", used: "2.00", reserved: "1.00", available: "7.00" }] }] },
          { type: "policy_citations", citations: [{ number: 1, chunk_id: "chunk-1", document_name: "员工休假制度.pdf", page_number: 3, evidence_snapshot: "年假申请应提前提交。" }] },
          { type: "clarification", missing_fields: ["start_date"], suggestions: ["从 8 月 20 日开始"] },
        ],
        model_calls: 1,
        read_calls: 2,
        write_proposals: 0,
      }],
    });
    const user = userEvent.setup();
    render(<HrAssistantPage />);

    expect(await screen.findByRole("heading", { name: "年假余额" })).toBeInTheDocument();
    expect(screen.getByText("查询我的年假余额")).toBeInTheDocument();
    expect(screen.getByText("HR 业务数据")).toBeInTheDocument();
    expect(screen.getByText("制度依据")).toBeInTheDocument();
    expect(screen.getByText("员工休假制度.pdf · 第 3 页")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "从 8 月 20 日开始" }));
    expect(screen.getByRole("textbox", { name: "向 AI 请假助手说明事项" })).toHaveValue("从 8 月 20 日开始");
  });

  it("confirms once with a retained operation id and renders only a server execution result as submitted", async () => {
    const confirmationId = "30000000-0000-4000-8000-000000000001";
    vi.mocked(hrClient.getConversation).mockResolvedValue({
      ...conversation,
      turns: [{
        client_turn_id: "20000000-0000-4000-8000-000000000002",
        text: "我想请年假",
        created_at: "2026-08-16T08:03:00Z",
        blocks: [{ type: "confirmation", confirmation_id: confirmationId, tool_name: "hr.submit_leave_request", expires_at: "2099-08-16T09:00:00Z", preview: { leave_type_code: "annual", start_date: "2026-08-20", end_date: "2026-08-21", workday_count: "2.00", available_before: "7.00", available_after: "5.00", reason: "家庭事务" } }],
        model_calls: 1,
        read_calls: 0,
        write_proposals: 1,
      }],
    });
    let release!: (value: Awaited<ReturnType<typeof hrClient.confirmTool>>) => void;
    vi.mocked(hrClient.confirmTool).mockReturnValue(new Promise(resolve => { release = resolve; }));
    const user = userEvent.setup();
    render(<HrAssistantPage />);

    const confirm = await screen.findByRole("button", { name: "确认提交" });
    await user.click(confirm);
    expect(confirm).toBeDisabled();
    expect(hrClient.confirmTool).toHaveBeenCalledWith(confirmationId, expect.any(String));
    const operationId = vi.mocked(hrClient.confirmTool).mock.calls[0][1];
    expect(screen.queryByText("已提交")).not.toBeInTheDocument();
    release({ type: "execution_result", resource_type: "leave_request", resource_id: "r1", result: { id: "r1", request_number: "LR-2026-001", leave_type_code: "annual", leave_type_name: "年假", start_date: "2026-08-20", end_date: "2026-08-21", workday_count: "2.00", reason: "家庭事务", status: "pending", submitted_at: "2026-08-16T08:04:00Z", reviewed_at: null, rejection_reason: null, cancelled_at: null } });

    expect(await screen.findByText("已提交，等待 HR 审核")).toBeInTheDocument();
    expect(screen.getByText("LR-2026-001")).toBeInTheDocument();
    expect(operationId).toMatch(/^[0-9a-f-]{36}$/i);
  });

  it("retries an uncertain confirmation with the same operation id", async () => {
    const confirmationId = "30000000-0000-4000-8000-000000000009";
    vi.mocked(hrClient.getConversation).mockResolvedValue({
      ...conversation,
      turns: [{
        client_turn_id: "20000000-0000-4000-8000-000000000009",
        text: "提交年假申请",
        created_at: "2026-08-16T08:03:00Z",
        blocks: [{ type: "confirmation", confirmation_id: confirmationId, tool_name: "hr.submit_leave_request", expires_at: "2099-08-16T09:00:00Z", preview: { start_date: "2026-08-20" } }],
        model_calls: 1,
        read_calls: 0,
        write_proposals: 1,
      }],
    });
    vi.mocked(hrClient.confirmTool)
      .mockRejectedValueOnce(new Error("connection reset"))
      .mockResolvedValueOnce({ type: "execution_result", resource_type: "leave_request", resource_id: "r9", result: { request_number: "LR-2026-009", status: "pending" } });
    const user = userEvent.setup();
    render(<HrAssistantPage />);

    await user.click(await screen.findByRole("button", { name: "确认提交" }));
    expect(await screen.findByRole("button", { name: "重试确认" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "重试确认" }));
    expect(await screen.findByText("LR-2026-009")).toBeInTheDocument();
    expect(hrClient.confirmTool).toHaveBeenCalledTimes(2);
    expect(vi.mocked(hrClient.confirmTool).mock.calls[0][1]).toBe(vi.mocked(hrClient.confirmTool).mock.calls[1][1]);
  });

  it("does not offer repeated confirmation after a deterministic execution failure", async () => {
    const confirmationId = "30000000-0000-4000-8000-000000000010";
    vi.mocked(hrClient.getConversation).mockResolvedValue({
      ...conversation,
      turns: [{
        client_turn_id: "20000000-0000-4000-8000-000000000010",
        text: "提交调休申请",
        created_at: "2026-08-16T08:03:00Z",
        blocks: [{ type: "confirmation", confirmation_id: confirmationId, tool_name: "hr.submit_leave_request", expires_at: "2099-08-16T09:00:00Z", preview: { leave_type_code: "compensatory", start_date: "2026-10-01" } }],
        model_calls: 1,
        read_calls: 0,
        write_proposals: 1,
      }],
    });
    vi.mocked(hrClient.confirmTool).mockRejectedValue(
      new ApiError(409, "tool_execution_non_retryable", {
        code: "tool_execution_non_retryable",
      }),
    );
    const user = userEvent.setup();
    render(<HrAssistantPage />);

    await user.click(await screen.findByRole("button", { name: "确认提交" }));

    expect(await screen.findByText("本次确认无法通过重复点击恢复，请返回修改并重新发起。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "无法重试" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "返回修改" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "重试确认" })).not.toBeInTheDocument();
    expect(hrClient.confirmTool).toHaveBeenCalledTimes(1);
  });

  it("cancels without executing and disables expired confirmations", async () => {
    vi.mocked(hrClient.getConversation).mockResolvedValue({
      ...conversation,
      turns: [{
        client_turn_id: "20000000-0000-4000-8000-000000000003",
        text: "我想请年假",
        created_at: "2026-08-16T08:03:00Z",
        blocks: [
          { type: "confirmation", confirmation_id: "active", tool_name: "hr.submit_leave_request", expires_at: "2099-08-16T09:00:00Z", preview: { start_date: "2026-08-20" } },
          { type: "confirmation", confirmation_id: "expired", tool_name: "hr.submit_leave_request", expires_at: "2020-01-01T00:00:00Z", preview: { start_date: "2026-08-22" } },
        ],
        model_calls: 1,
        read_calls: 0,
        write_proposals: 1,
      }],
    });
    const user = userEvent.setup();
    render(<HrAssistantPage />);

    expect(await screen.findByText("确认已过期，请重新发起")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "确认提交" })[1]).toBeDisabled();
    await user.click(screen.getAllByRole("button", { name: "返回修改" })[0]);
    expect(hrClient.cancelTool).toHaveBeenCalledWith("active");
    expect(await screen.findByText("已取消，本次没有提交申请")).toBeInTheDocument();
    expect(hrClient.confirmTool).not.toHaveBeenCalled();
  });

  it("renders projected cancelled expired and executed history as non-actionable", async () => {
    vi.mocked(hrClient.getConversation).mockResolvedValue({
      ...conversation,
      turns: [{
        client_turn_id: "20000000-0000-4000-8000-000000000013",
        text: "刷新后的确认历史",
        created_at: "2026-08-16T08:03:00Z",
        blocks: [
          {
            type: "confirmation_terminal",
            confirmation_id: "30000000-0000-4000-8000-000000000013",
            tool_name: "hr.submit_leave_request",
            status: "cancelled",
          },
          {
            type: "confirmation_terminal",
            confirmation_id: "30000000-0000-4000-8000-000000000014",
            tool_name: "hr.submit_leave_request",
            status: "expired",
          },
          {
            type: "execution_result",
            resource_type: "leave_request",
            resource_id: "40000000-0000-4000-8000-000000000013",
            result: {
              request_number: "LR-2026-013",
              status: "pending",
            },
          },
        ],
        model_calls: 1,
        read_calls: 0,
        write_proposals: 1,
      }],
    } as unknown as HrConversationDetail);
    render(<HrAssistantPage />);

    expect(await screen.findByText("已取消，本次没有提交申请")).toBeInTheDocument();
    expect(screen.getByText("确认已过期，请重新发起")).toBeInTheDocument();
    expect(screen.getByText("LR-2026-013")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认提交" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "返回修改" })).not.toBeInTheDocument();
    expect(hrClient.confirmTool).not.toHaveBeenCalled();
    expect(hrClient.cancelTool).not.toHaveBeenCalled();
  });

  it("offers retry for provider failures and refreshes stable conflict states", async () => {
    vi.mocked(hrClient.sendTurn)
      .mockResolvedValueOnce(response([{ type: "error", code: "tool_provider_unavailable", retryable: true }]))
      .mockResolvedValueOnce(response([{ type: "text", text: "服务已经恢复" }]));
    const user = userEvent.setup();
    render(<HrAssistantPage />);
    const composer = await screen.findByRole("textbox", { name: "向 AI 请假助手说明事项" });
    await user.type(composer, "查年假{Enter}");

    expect(await screen.findByText("HR 智能服务暂时不可用，未执行任何写操作。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试本次请求" }));
    expect(await screen.findByText("服务已经恢复")).toBeInTheDocument();

    vi.mocked(hrClient.sendTurn).mockRejectedValueOnce(Object.assign(new Error("conflict"), { status: 409, code: "client_turn_id_conflict" }));
    await user.type(composer, "再查一次{Enter}");
    expect(await screen.findByText("状态已发生变化，已为你刷新当前对话。")).toBeInTheDocument();
    expect(hrClient.getConversation).toHaveBeenCalledTimes(2);
  });

  it("announces loading and completion through a polite live region", async () => {
    let release!: (value: HrTurnResponse) => void;
    vi.mocked(hrClient.sendTurn).mockReturnValue(new Promise(resolve => { release = resolve; }));
    const user = userEvent.setup();
    render(<HrAssistantPage />);
    const composer = await screen.findByRole("textbox", { name: "向 AI 请假助手说明事项" });
    await user.type(composer, "查询余额{Enter}");

    expect(screen.getByRole("status")).toHaveTextContent("正在处理你的事项");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    release(response([{ type: "text", text: "余额已返回" }]));
    expect(await screen.findByText("余额已返回")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("处理完成");
  });
});
