import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as workbenchClient from "./client";
import { AnalyticsDashboardPage } from "./AnalyticsDashboardPage";
import type {
  AnalyticsDashboardData,
  AnalyticsResponse,
  MetricValue,
} from "./types";

vi.mock("./client", () => ({
  loadAnalyticsDashboard: vi.fn(),
  listOrganizationUnits: vi.fn(),
}));

function metric(
  value: number | null,
  options: Partial<MetricValue> = {},
): MetricValue {
  return {
    numerator: value,
    denominator: null,
    value,
    available: value !== null,
    sample_size: value === null ? 0 : 12,
    metric_version: "v1",
    ...options,
  };
}

function response(metrics: Record<string, MetricValue>): AnalyticsResponse {
  return {
    from: "2026-08-10T00:00:00Z",
    to: "2026-08-17T00:00:00Z",
    organization_unit_id: null,
    metric_version: "v1",
    metrics,
  };
}

function dashboard(questionCount = 42): AnalyticsDashboardData {
  return {
    overview: response({
      questions_submitted: metric(questionCount),
      leave_requests_submitted: metric(8),
      tools_planned: metric(18),
      leave_requests_pending: metric(3),
    }),
    knowledge: response({
      evidence_answer_rate: metric(0.75, {
        numerator: 9,
        denominator: 12,
      }),
      clarification_rate: metric(0.25, {
        numerator: 3,
        denominator: 12,
      }),
      abstention_rate: metric(null, { sample_size: 3 }),
      response_latency_p50_ms: metric(350),
      response_latency_p95_ms: metric(575),
    }),
    hrFunnel: response({
      hr_turns_submitted: metric(12),
      intents_resolved: metric(10),
      write_tools_planned: metric(7),
      confirmations_shown: metric(6),
      confirmations_confirmed: metric(5),
      leave_requests_submitted: metric(5),
      leave_requests_reviewed: metric(4),
    }),
    tools: response({
      validation_failure_rate: metric(0.1, {
        numerator: 1,
        denominator: 10,
      }),
      provider_failure_rate: metric(0),
      read_latency_p50_ms: metric(180),
      read_latency_p95_ms: metric(420),
    }),
    workflows: response({
      leave_requests_pending: metric(3),
      leave_requests_processed: metric(7),
      review_duration_p50_ms: metric(10_800_000),
      review_duration_p95_ms: metric(17_280_000),
      leave_requests_pending_over_24h: metric(1),
    }),
  };
}

beforeEach(() => {
  vi.mocked(workbenchClient.listOrganizationUnits).mockResolvedValue([
    {
      id: "10000000-0000-4000-8000-000000000001",
      code: "PRODUCT",
      name: "产品中心",
      parent_id: null,
      is_active: true,
    },
  ]);
  vi.mocked(workbenchClient.loadAnalyticsDashboard).mockResolvedValue(dashboard());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("operations analytics dashboard", () => {
  it("renders four fixed metric groups with honest availability and definitions", async () => {
    render(<AnalyticsDashboardPage />);

    expect(screen.getByRole("status")).toHaveTextContent("正在汇总运营指标");
    expect(
      await screen.findByRole("heading", { name: "运营驾驶舱" }),
    ).toBeInTheDocument();
    for (const heading of ["知识质量", "HR 流程", "Tool Calling", "审批工作流"]) {
      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
    }
    for (const stage of [
      "HR 对话提交",
      "意图识别完成",
      "写工具计划",
      "展示确认",
      "用户已确认",
      "请假已审核",
    ]) {
      expect(screen.getByText(stage)).toBeInTheDocument();
    }
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("9 / 12")).toBeInTheDocument();
    expect(screen.getByText("需要补充信息率")).toBeInTheDocument();
    expect(screen.getByText("3 / 12")).toBeInTheDocument();
    expect(screen.getAllByText(/样本 12/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/口径 v1/).length).toBeGreaterThan(0);
    expect(screen.getByText("暂无数据")).toBeInTheDocument();
    expect(screen.getByText(/样本量 3，低于组织细分展示阈值/)).toBeInTheDocument();
    expect(screen.queryByText("100%")).not.toBeInTheDocument();
    expect(screen.getByText("350 ms")).toBeInTheDocument();
    expect(screen.getByText("3 小时")).toBeInTheDocument();
  });

  it("switches 7/30/90 day ranges, filters organizations, and ignores stale responses", async () => {
    let resolveFirst: ((value: AnalyticsDashboardData) => void) | undefined;
    let resolveSecond: ((value: AnalyticsDashboardData) => void) | undefined;
    vi.mocked(workbenchClient.loadAnalyticsDashboard)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve; }))
      .mockResolvedValue(dashboard(90));
    const user = userEvent.setup();
    render(<AnalyticsDashboardPage />);

    await waitFor(() => {
      expect(workbenchClient.loadAnalyticsDashboard).toHaveBeenCalledTimes(1);
    });
    await user.click(screen.getByRole("button", { name: "最近 30 天" }));
    await waitFor(() => {
      expect(workbenchClient.loadAnalyticsDashboard).toHaveBeenCalledTimes(2);
    });
    resolveSecond?.(dashboard(30));
    expect(await screen.findByText("30")).toBeInTheDocument();
    resolveFirst?.(dashboard(7));
    await waitFor(() => {
      expect(
        within(screen.getByLabelText("核心概览")).queryByText("7"),
      ).not.toBeInTheDocument();
    });

    await user.selectOptions(screen.getByLabelText("组织范围"), "10000000-0000-4000-8000-000000000001");
    await waitFor(() => {
      expect(workbenchClient.loadAnalyticsDashboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ days: 30, organizationUnitId: "10000000-0000-4000-8000-000000000001" }),
      );
    });
    expect(screen.getByRole("button", { name: "最近 7 天" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "最近 90 天" })).toBeInTheDocument();
  });

  it("shows a retryable error without turning failed data into empty metrics", async () => {
    vi.mocked(workbenchClient.loadAnalyticsDashboard)
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(dashboard());
    const user = userEvent.setup();
    render(<AnalyticsDashboardPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("运营指标暂时无法读取");
    expect(screen.queryByText("暂无数据")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("heading", { name: "运营驾驶舱" })).toBeInTheDocument();
  });
});
