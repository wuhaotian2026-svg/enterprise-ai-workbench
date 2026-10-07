import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/request";
import * as procurementClient from "./client";
import { ProcurementAssistantTurn } from "./ProcurementAssistantTurn";
import type { ProcurementTurnResponse } from "./types";

vi.mock("./client", () => ({
  newProcurementOperationId: vi.fn(() => "90000000-0000-4000-8000-000000000001"),
  confirmProcurementSubmission: vi.fn(),
  cancelProcurementConfirmation: vi.fn(),
}));

const confirmationTurn: ProcurementTurnResponse = {
  id: "50000000-0000-4000-8000-000000000001",
  client_turn_id: "60000000-0000-4000-8000-000000000001",
  role: "assistant",
  text: "请确认采购申请。",
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
    expires_at: "2026-08-26T13:00:00Z",
  }],
  created_at: "2026-08-26T12:00:00Z",
  replayed: false,
};

describe("ProcurementAssistantTurn", () => {
  beforeEach(() => {
    vi.mocked(procurementClient.newProcurementOperationId).mockReset();
    vi.mocked(procurementClient.newProcurementOperationId)
      .mockReturnValue("90000000-0000-4000-8000-000000000001");
    vi.mocked(procurementClient.confirmProcurementSubmission).mockReset();
    vi.mocked(procurementClient.cancelProcurementConfirmation).mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("renders a proposal-only procurement confirmation from an explicit preview whitelist", () => {
    render(<ProcurementAssistantTurn turn={confirmationTurn} now={() => new Date("2026-08-26T12:30:00Z")} />);

    expect(screen.getByRole("group", { name: "采购 AI 提交确认" })).toBeInTheDocument();
    expect(screen.getByText("研发电脑")).toBeInTheDocument();
    expect(screen.getByText("用于移动端研发")).toBeInTheDocument();
    expect(screen.getByText("¥12999.00 CNY")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认提交 AI 采购申请" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "返回修改 AI 采购申请" })).toBeEnabled();
    expect(screen.queryByText("must-not-render")).not.toBeInTheDocument();
    expect(procurementClient.confirmProcurementSubmission).not.toHaveBeenCalled();
  });

  it("confirms once with a caller-owned operation ID and renders the execution terminal state", async () => {
    vi.mocked(procurementClient.confirmProcurementSubmission).mockResolvedValue({
      type: "execution_result",
      resource_type: "procurement_request",
      resource_id: "80000000-0000-4000-8000-000000000001",
      replayed: false,
    });
    const user = userEvent.setup();
    render(<ProcurementAssistantTurn turn={confirmationTurn} now={() => new Date("2026-08-26T12:30:00Z")} />);

    await user.click(screen.getByRole("button", { name: "确认提交 AI 采购申请" }));

    await waitFor(() => expect(procurementClient.confirmProcurementSubmission).toHaveBeenCalledWith(
      "70000000-0000-4000-8000-000000000001",
      "90000000-0000-4000-8000-000000000001",
    ));
    expect(screen.getByRole("status")).toHaveTextContent("采购申请已创建，审批流程已启动");
    expect(screen.getByRole("button", { name: "确认提交 AI 采购申请" })).toBeDisabled();
    expect(screen.queryByText("80000000-0000-4000-8000-000000000001")).not.toBeInTheDocument();
  });

  it("cancels the proposal without executing it", async () => {
    vi.mocked(procurementClient.cancelProcurementConfirmation).mockResolvedValue({
      confirmation_id: "70000000-0000-4000-8000-000000000001",
      status: "cancelled",
    });
    const user = userEvent.setup();
    render(<ProcurementAssistantTurn turn={confirmationTurn} now={() => new Date("2026-08-26T12:30:00Z")} />);

    await user.click(screen.getByRole("button", { name: "返回修改 AI 采购申请" }));

    await waitFor(() => expect(procurementClient.cancelProcurementConfirmation).toHaveBeenCalledWith(
      "70000000-0000-4000-8000-000000000001",
    ));
    expect(procurementClient.confirmProcurementSubmission).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("已取消这次写操作提案");
    expect(screen.getByRole("button", { name: "返回修改 AI 采购申请" })).toBeDisabled();
  });

  it("prevents duplicate confirmation while busy", async () => {
    let settle!: () => void;
    vi.mocked(procurementClient.confirmProcurementSubmission).mockImplementation(() => new Promise(resolve => {
      settle = () => resolve({
        type: "execution_result",
        resource_type: "procurement_request",
        resource_id: "80000000-0000-4000-8000-000000000001",
        replayed: false,
      });
    }));
    render(<ProcurementAssistantTurn turn={confirmationTurn} now={() => new Date("2026-08-26T12:30:00Z")} />);
    const confirm = screen.getByRole("button", { name: "确认提交 AI 采购申请" });

    fireEvent.click(confirm);
    fireEvent.click(confirm);

    expect(procurementClient.confirmProcurementSubmission).toHaveBeenCalledTimes(1);
    expect(confirm).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("正在确认提交");
    settle();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("采购申请已创建"));
  });

  it("reuses the same operation ID for an ambiguous confirmation retry", async () => {
    vi.mocked(procurementClient.confirmProcurementSubmission)
      .mockRejectedValueOnce(new TypeError("network interrupted"))
      .mockResolvedValueOnce({
        type: "execution_result",
        resource_type: "procurement_request",
        resource_id: "80000000-0000-4000-8000-000000000001",
        replayed: true,
      });
    const user = userEvent.setup();
    render(<ProcurementAssistantTurn turn={confirmationTurn} now={() => new Date("2026-08-26T12:30:00Z")} />);

    await user.click(screen.getByRole("button", { name: "确认提交 AI 采购申请" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("结果尚未确认，可以安全重试");
    await user.click(screen.getByRole("button", { name: "安全重试提交 AI 采购申请" }));

    await waitFor(() => expect(procurementClient.confirmProcurementSubmission).toHaveBeenCalledTimes(2));
    expect(vi.mocked(procurementClient.confirmProcurementSubmission).mock.calls[0]?.[1])
      .toBe(vi.mocked(procurementClient.confirmProcurementSubmission).mock.calls[1]?.[1]);
    expect(procurementClient.newProcurementOperationId).toHaveBeenCalledTimes(1);
  });

  it("disables an expired proposal and performs no request", async () => {
    const user = userEvent.setup();
    render(<ProcurementAssistantTurn turn={confirmationTurn} now={() => new Date("2026-08-26T13:00:00Z")} />);

    expect(screen.getByRole("alert")).toHaveTextContent("此确认已过期");
    const confirm = screen.getByRole("button", { name: "确认提交 AI 采购申请" });
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(procurementClient.confirmProcurementSubmission).not.toHaveBeenCalled();
    expect(procurementClient.newProcurementOperationId).not.toHaveBeenCalled();
  });

  it("keeps a 409 conflict in a visible failed state instead of reporting success", async () => {
    vi.mocked(procurementClient.confirmProcurementSubmission)
      .mockRejectedValue(new ApiError(409, "operation_id_conflict"));
    const user = userEvent.setup();
    render(<ProcurementAssistantTurn turn={confirmationTurn} now={() => new Date("2026-08-26T12:30:00Z")} />);

    await user.click(screen.getByRole("button", { name: "确认提交 AI 采购申请" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("提交未完成");
    expect(screen.queryByText("采购申请已创建")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认提交 AI 采购申请" })).toBeDisabled();
  });

  it("preserves ordinary assistant text without inventing a confirmation", () => {
    render(<ProcurementAssistantTurn turn={{
      ...confirmationTurn,
      text: "制度要求说明采购用途。",
      blocks: [{ type: "text", text: "制度要求说明采购用途。" }],
    }} />);

    expect(screen.getByText("制度要求说明采购用途。")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "采购 AI 提交确认" })).not.toBeInTheDocument();
    expect(procurementClient.newProcurementOperationId).not.toHaveBeenCalled();
  });
});
