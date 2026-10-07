import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/request";
import * as hrClient from "./client";
import { MyLeaveRequestsPage } from "./MyLeaveRequestsPage";
import type { ConfirmationBlock, ExecutionResultBlock, LeaveRequest } from "./types";

vi.mock("./client", () => ({
  listLeaveRequests: vi.fn(),
  createCancelIntent: vi.fn(),
  confirmTool: vi.fn(),
  cancelTool: vi.fn(),
  newClientId: vi.fn(() => crypto.randomUUID()),
}));

const pending: LeaveRequest = {
  id: "10000000-0000-4000-8000-000000000001",
  request_number: "LR-2026-001",
  leave_type_code: "annual",
  leave_type_name: "年假",
  start_date: "2026-08-20",
  end_date: "2026-08-21",
  workday_count: "2.00",
  reason: "家庭事务",
  status: "pending",
  submitted_at: "2026-08-16T08:00:00Z",
  reviewed_at: null,
  rejection_reason: null,
  cancelled_at: null,
};

const approved: LeaveRequest = {
  ...pending,
  id: "10000000-0000-4000-8000-000000000002",
  request_number: "LR-2026-002",
  start_date: "2026-07-10",
  end_date: "2026-07-10",
  workday_count: "1.00",
  status: "approved",
  reviewed_at: "2026-07-08T09:00:00Z",
};

const cancellation: ConfirmationBlock = {
  type: "confirmation",
  confirmation_id: "20000000-0000-4000-8000-000000000001",
  tool_name: "hr.cancel_leave_request",
  expires_at: "2099-08-16T09:00:00Z",
  preview: pending,
};

const cancelledResult: ExecutionResultBlock = {
  type: "execution_result",
  resource_type: "leave_request",
  resource_id: pending.id,
  result: { ...pending, status: "cancelled" },
};

beforeEach(() => {
  vi.mocked(hrClient.listLeaveRequests).mockResolvedValue([pending, approved]);
  vi.mocked(hrClient.createCancelIntent).mockResolvedValue(cancellation);
  vi.mocked(hrClient.confirmTool).mockResolvedValue(cancelledResult);
  vi.mocked(hrClient.cancelTool).mockResolvedValue({ confirmation_id: cancellation.confirmation_id, status: "cancelled" });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("my leave requests", () => {
  it("filters by status and date while preserving explicit text status cues", async () => {
    const user = userEvent.setup();
    render(<MyLeaveRequestsPage />);

    expect(await screen.findByText("LR-2026-001")).toBeInTheDocument();
    expect(screen.getAllByText("待审批").length).toBeGreaterThan(1);
    expect(screen.getAllByText("已批准").length).toBeGreaterThan(1);
    await user.selectOptions(screen.getByRole("combobox", { name: "申请状态" }), "approved");
    expect(screen.queryByText("LR-2026-001")).not.toBeInTheDocument();
    expect(screen.getByText("LR-2026-002")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "申请状态" }), "all");
    await user.type(screen.getByLabelText("开始日期不早于"), "2026-08-01");
    expect(screen.getByText("LR-2026-001")).toBeInTheDocument();
    expect(screen.queryByText("LR-2026-002")).not.toBeInTheDocument();
  });

  it("uses a server confirmation for pending-only cancellation and refreshes after execution", async () => {
    const user = userEvent.setup();
    render(<MyLeaveRequestsPage />);

    await user.click(await screen.findByRole("button", { name: "撤销 LR-2026-001" }));
    expect(hrClient.createCancelIntent).toHaveBeenCalledWith(pending.id, expect.any(String));
    expect(screen.getByRole("heading", { name: "撤销申请确认" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "撤销 LR-2026-002" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认撤销" }));
    expect(hrClient.confirmTool).toHaveBeenCalledWith(cancellation.confirmation_id, expect.any(String));
    await waitFor(() => expect(hrClient.listLeaveRequests).toHaveBeenCalledTimes(2));
  });

  it("refreshes real state after a cancellation conflict", async () => {
    vi.mocked(hrClient.confirmTool).mockRejectedValue(new ApiError(409, "leave_request_state_conflict"));
    const user = userEvent.setup();
    render(<MyLeaveRequestsPage />);

    await user.click(await screen.findByRole("button", { name: "撤销 LR-2026-001" }));
    await user.click(screen.getByRole("button", { name: "确认撤销" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("状态已变化");
    expect(hrClient.listLeaveRequests).toHaveBeenCalledTimes(2);
  });

  it("does not offer repeated cancellation after a deterministic execution failure", async () => {
    vi.mocked(hrClient.confirmTool).mockRejectedValue(
      new ApiError(409, "tool_execution_non_retryable"),
    );
    const user = userEvent.setup();
    render(<MyLeaveRequestsPage />);

    await user.click(await screen.findByRole("button", { name: "撤销 LR-2026-001" }));
    await user.click(screen.getByRole("button", { name: "确认撤销" }));

    expect(await screen.findByText("本次确认无法通过重复点击恢复，请返回修改并重新发起。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "无法重试" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "返回修改" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "重试确认" })).not.toBeInTheDocument();
    expect(hrClient.confirmTool).toHaveBeenCalledTimes(1);
  });

  it("renders safe loading, empty, and error states", async () => {
    vi.mocked(hrClient.listLeaveRequests).mockResolvedValueOnce([]);
    render(<MyLeaveRequestsPage />);
    expect(screen.getByRole("status")).toHaveTextContent("正在读取");
    expect(await screen.findByText(/没有符合条件的请假申请/)).toBeInTheDocument();

    cleanup();
    vi.mocked(hrClient.listLeaveRequests).mockRejectedValueOnce(new Error("secret upstream detail"));
    render(<MyLeaveRequestsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法读取申请记录");
    expect(screen.queryByText("secret upstream detail")).not.toBeInTheDocument();
  });
});
