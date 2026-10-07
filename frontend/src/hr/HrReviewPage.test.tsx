import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/request";
import * as hrClient from "./client";
import { HrReviewPage } from "./HrReviewPage";
import type { HrReviewRequest } from "./types";

vi.mock("./client", () => ({
  listReviewQueue: vi.fn(),
  getReviewDetail: vi.fn(),
  approveLeaveRequest: vi.fn(),
  rejectLeaveRequest: vi.fn(),
  newClientId: vi.fn(() => crypto.randomUUID()),
}));

const request: HrReviewRequest = {
  id: "30000000-0000-4000-8000-000000000001",
  request_number: "LR-2026-009",
  leave_type_code: "annual",
  leave_type_name: "年假",
  start_date: "2026-08-20",
  end_date: "2026-08-21",
  workday_count: "2.00",
  reason: "家庭事务",
  status: "pending",
  submitted_at: "2026-08-16T08:00:00Z",
  reviewed_at: null,
  reviewer_user_id: null,
  rejection_reason: null,
  cancelled_at: null,
  employee_number: "E-1001",
  employee_display_name: "Alice 示例员工",
};

beforeEach(() => {
  vi.mocked(hrClient.listReviewQueue).mockResolvedValue([request]);
  vi.mocked(hrClient.getReviewDetail).mockResolvedValue(request);
  vi.mocked(hrClient.approveLeaveRequest).mockResolvedValue({ ...request, status: "approved", reviewed_at: "2026-08-16T09:00:00Z" });
  vi.mocked(hrClient.rejectLeaveRequest).mockResolvedValue({ ...request, status: "rejected", rejection_reason: "业务高峰期" });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("deterministic HR review", () => {
  it("loads employee-aware pending work and approves without invoking a planner", async () => {
    const user = userEvent.setup();
    render(<HrReviewPage />);

    expect(await screen.findByText("Alice 示例员工")).toBeInTheDocument();
    expect(screen.getByText("E-1001")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "审核 LR-2026-009" }));
    expect(hrClient.getReviewDetail).toHaveBeenCalledWith(request.id);
    await user.click(await screen.findByRole("button", { name: "批准申请" }));
    expect(hrClient.approveLeaveRequest).toHaveBeenCalledWith(request.id, expect.any(String));
    await waitFor(() => expect(hrClient.listReviewQueue).toHaveBeenCalledTimes(2));
  });

  it("requires a rejection reason and disables duplicate review submits", async () => {
    let resolveReject: ((value: HrReviewRequest) => void) | undefined;
    vi.mocked(hrClient.rejectLeaveRequest).mockImplementation(() => new Promise(resolve => { resolveReject = resolve; }));
    const user = userEvent.setup();
    render(<HrReviewPage />);

    await user.click(await screen.findByRole("button", { name: "审核 LR-2026-009" }));
    await user.click(await screen.findByRole("button", { name: "驳回申请" }));
    const submit = screen.getByRole("button", { name: "确认驳回" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "驳回原因" }), "业务高峰期");
    await user.click(submit);
    expect(submit).toBeDisabled();
    await user.click(submit);
    expect(hrClient.rejectLeaveRequest).toHaveBeenCalledTimes(1);
    expect(hrClient.rejectLeaveRequest).toHaveBeenCalledWith(request.id, expect.any(String), "业务高峰期");
    resolveReject?.({ ...request, status: "rejected", rejection_reason: "业务高峰期" });
  });

  it("refreshes the queue after a state conflict", async () => {
    vi.mocked(hrClient.approveLeaveRequest).mockRejectedValue(new ApiError(409, "leave_request_state_conflict"));
    const user = userEvent.setup();
    render(<HrReviewPage />);

    await user.click(await screen.findByRole("button", { name: "审核 LR-2026-009" }));
    await user.click(await screen.findByRole("button", { name: "批准申请" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("状态已变化");
    expect(hrClient.listReviewQueue).toHaveBeenCalledTimes(2);
  });

  it("reuses the approval operation id after an uncertain transport failure", async () => {
    vi.mocked(hrClient.approveLeaveRequest)
      .mockRejectedValueOnce(new Error("connection reset"))
      .mockResolvedValueOnce({ ...request, status: "approved" });
    const user = userEvent.setup();
    render(<HrReviewPage />);

    await user.click(await screen.findByRole("button", { name: "审核 LR-2026-009" }));
    const approve = await screen.findByRole("button", { name: "批准申请" });
    await user.click(approve);
    expect(await screen.findByRole("alert")).toHaveTextContent("批准未完成");
    await user.click(approve);

    expect(hrClient.approveLeaveRequest).toHaveBeenCalledTimes(2);
    expect(vi.mocked(hrClient.approveLeaveRequest).mock.calls[1][1])
      .toBe(vi.mocked(hrClient.approveLeaveRequest).mock.calls[0][1]);
  });

  it("supports status filters and safe empty/error states", async () => {
    const user = userEvent.setup();
    vi.mocked(hrClient.listReviewQueue).mockResolvedValueOnce([]);
    render(<HrReviewPage />);
    expect(screen.getByRole("status")).toHaveTextContent("正在读取");
    expect(await screen.findByText(/当前没有待处理申请/)).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "审核状态" }), "approved");
    expect(hrClient.listReviewQueue).toHaveBeenLastCalledWith("approved");

    cleanup();
    vi.mocked(hrClient.listReviewQueue).mockRejectedValueOnce(new Error("database secret"));
    render(<HrReviewPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法读取审核队列");
    expect(screen.queryByText("database secret")).not.toBeInTheDocument();
  });
});
