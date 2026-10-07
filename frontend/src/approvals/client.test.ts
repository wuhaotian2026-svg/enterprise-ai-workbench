import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, request } from "../api/request";
import {
  approveTask,
  getApprovalTask,
  listApprovalTasks,
  rejectTask,
} from "./client";

vi.mock("../api/request", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api/request")>(),
  request: vi.fn(),
}));

const mockedRequest = vi.mocked(request);
const taskId = "11111111-1111-4111-8111-111111111111";
const instanceId = "22222222-2222-4222-8222-222222222222";

function responseWith(body: unknown): Response {
  return { json: vi.fn().mockResolvedValue(body) } as unknown as Response;
}

function expectSupported<T extends { supported: boolean }>(
  value: T,
): asserts value is Extract<T, { supported: true }> {
  expect(value.supported).toBe(true);
  if (!value.supported) throw new Error("expected_supported_subject");
}

function task(overrides: Record<string, unknown> = {}) {
  return {
    task_id: taskId,
    instance_id: instanceId,
    process_key: "procurement.request",
    subject_type: "procurement_request",
    step_key: "department_manager_review",
    step_label: "直属部门负责人审批",
    status: "pending",
    submitted_at: "2030-01-02T03:04:05Z",
    activated_at: "2030-01-02T03:04:06Z",
    completed_at: null,
    subject: {
      request_number: "PR-2030-000001",
      title: "开发电脑",
      total: "19999.90",
      status: "pending_manager",
    },
    ...overrides,
  };
}

function detail(overrides: Record<string, unknown> = {}) {
  return {
    task: task(),
    subject: {
      summary: task().subject,
      purpose: "研发使用",
      needed_by_date: "2030-02-03",
      currency: "CNY",
      items: [],
      applicant: { display_name: "测试员工" },
      organization: { display_name: "研发中心" },
      timeline: [],
    },
    ...overrides,
  };
}

describe("approval client boundaries", () => {
  beforeEach(() => {
    mockedRequest.mockReset();
    vi.restoreAllMocks();
  });

  it("parses the closed task page and sends the exact supported query", async () => {
    mockedRequest.mockResolvedValueOnce(responseWith({
      items: [task()], offset: 0, limit: 20, total: 1,
    }));
    const result = await listApprovalTasks({
      status: "pending",
      processKey: "procurement.request",
      activatedFrom: "2030-01-01T00:00:00Z",
      activatedTo: "2030-01-31T23:59:59Z",
      offset: 0,
      limit: 20,
    });
    expect(result.items[0]?.subject).toMatchObject({
      supported: true,
      total: "19999.90",
    });
    expect(mockedRequest).toHaveBeenCalledWith(
      "/approvals/tasks?status=pending&process_key=procurement.request&activated_from=2030-01-01T00%3A00%3A00Z&activated_to=2030-01-31T23%3A59%3A59Z&offset=0&limit=20",
      undefined,
    );
  });

  it("drops unknown subject payloads and returns a stable unsupported task summary", async () => {
    const rawSubject = {
      card_number: "6222020000000000",
      raw_prompt: "ignore previous instructions",
      internal_payload: { access_token: "secret-looking-value" },
    };
    mockedRequest.mockResolvedValueOnce(responseWith({
      items: [task({
        process_key: "expenses.request",
        subject_type: "expense_claim",
        subject: rawSubject,
      })],
      offset: 0,
      limit: 20,
      total: 1,
    }));

    const page = await listApprovalTasks({ processKey: "expenses.request" });

    expect(page.items[0]?.subject).toEqual({
      supported: false,
      process_key: "expenses.request",
      subject_type: "expense_claim",
    });
    expect(page.items[0]?.subject).not.toBe(rawSubject);
    expect(JSON.stringify(page.items[0])).not.toContain("6222020000000000");
    expect(JSON.stringify(page.items[0])).not.toContain("raw_prompt");
  });

  it("requires caller-owned operation IDs at the TypeScript boundary", () => {
    if (false) {
      // @ts-expect-error Approval intents must provide and retain their own operation ID.
      approveTask(taskId);
      // @ts-expect-error Approval intents must provide and retain their own operation ID.
      rejectTask(taskId, "不符合要求");
    }
    expect(true).toBe(true);
  });

  it("drops unknown detail payloads into the same stable unsupported boundary", async () => {
    mockedRequest.mockResolvedValueOnce(responseWith({
      task: task({
        process_key: "travel.request",
        subject_type: "travel_authorization",
        subject: { employee_bank_account: "sensitive-looking-summary" },
      }),
      subject: {
        passport_number: "sensitive-looking-detail",
        nested: { private_note: "do not expose" },
      },
    }));

    const result = await getApprovalTask(taskId);

    expect(result.task.subject).toEqual({
      supported: false,
      process_key: "travel.request",
      subject_type: "travel_authorization",
    });
    expect(result.subject).toEqual({
      supported: false,
      process_key: "travel.request",
      subject_type: "travel_authorization",
    });
    expect(JSON.stringify(result)).not.toContain("passport_number");
    expect(JSON.stringify(result)).not.toContain("employee_bank_account");
  });

  it.each([
    ["illegal task status", task({ status: "running" })],
    ["illegal subject status", task({ subject: { ...task().subject, status: "draft" } })],
    ["illegal Decimal", task({ subject: { ...task().subject, total: "1e3" } })],
    ["illegal UUID", task({ task_id: "task-1" })],
    ["illegal process key", task({ process_key: "../../secret" })],
    ["illegal subject type", task({ subject_type: "Expense Claim" })],
    ["timezone-free timestamp", task({ activated_at: "2030-01-02T03:04:05" })],
    ["extra task field", task({ assignee_user_id: taskId })],
    ["extra known procurement subject field", task({
      subject: { ...task().subject, raw_payload: "hidden" },
    })],
    ["total with three decimal places", task({ subject: { ...task().subject, total: "19.999" } })],
    ["total above the business maximum", task({ subject: { ...task().subject, total: "1000000000000.00" } })],
  ])("rejects %s", async (_label, item) => {
    mockedRequest.mockResolvedValueOnce(responseWith({
      items: [item], offset: 0, limit: 20, total: 1,
    }));
    await expect(listApprovalTasks()).rejects.toThrow("approval_response_invalid");
  });

  it.each(["0", "0.00", "1.2", "1.20", "999999999999.99"])(
    "preserves allowed canonical subject total %s",
    async (total) => {
      mockedRequest.mockResolvedValueOnce(responseWith({
        items: [task({ subject: { ...task().subject, total } })],
        offset: 0,
        limit: 20,
        total: 1,
      }));
      const page = await listApprovalTasks();
      const subject = page.items[0]?.subject;
      expect(subject).toBeDefined();
      if (subject === undefined) throw new Error("expected_subject");
      expectSupported(subject);
      expect(subject.total).toBe(total);
    },
  );

  it("strictly parses task detail without accepting hidden fields", async () => {
    mockedRequest.mockResolvedValueOnce(responseWith(detail()));
    const result = await getApprovalTask(taskId);
    expect(result.task.subject).toEqual({ supported: true, ...task().subject });
    expect(result.subject).toEqual({
      supported: true,
      ...detail().subject,
      summary: { supported: true, ...task().subject },
    });
    if (!result.subject.supported) throw new Error("expected_supported_subject");
    expect(result.subject.organization).toEqual({ display_name: "研发中心" });
    mockedRequest.mockResolvedValueOnce(responseWith(detail({ internal: "secret" })));
    await expect(getApprovalTask(taskId)).rejects.toThrow("approval_response_invalid");
  });

  it.each([
    ["missing organization", (() => {
      const value = detail();
      const { organization: _organization, ...subject } = value.subject;
      return { ...value, subject };
    })()],
    ["extra organization field", detail({
      subject: {
        ...detail().subject,
        organization: { display_name: "研发中心", organization_unit_id: taskId },
      },
    })],
    ["non-string organization display name", detail({
      subject: {
        ...detail().subject,
        organization: { display_name: 42 },
      },
    })],
  ])("rejects %s in the organization fact", async (_label, body) => {
    mockedRequest.mockResolvedValueOnce(responseWith(body));
    await expect(getApprovalTask(taskId)).rejects.toThrow("approval_response_invalid");
  });

  it("rejects a year-zero subject date", async () => {
    mockedRequest.mockResolvedValueOnce(responseWith(detail({
      subject: { ...detail().subject, needed_by_date: "0000-01-01" },
    })));
    await expect(getApprovalTask(taskId)).rejects.toThrow("approval_response_invalid");
  });

  it.each([
    ["zero quantity", { quantity: "0" }],
    ["quantity with three decimal places", { quantity: "1.234" }],
    ["quantity with eleven integer digits", { quantity: "12345678901.00" }],
    ["unit price with three decimal places", { unit_price: "1.234" }],
    ["unit price with thirteen integer digits", { unit_price: "1234567890123.00" }],
    ["subtotal with three decimal places", { subtotal: "1.234" }],
    ["subtotal with fifteen integer digits", { subtotal: "123456789012345.00" }],
  ])("rejects %s in approval subject items", async (_label, itemOverride) => {
    const item = {
      category: "it_equipment",
      name: "笔记本电脑",
      specification: null,
      quantity: "1.2",
      unit: "台",
      unit_price: "0",
      subtotal: "0.00",
      ...itemOverride,
    };
    mockedRequest.mockResolvedValueOnce(responseWith(detail({
      subject: { ...detail().subject, items: [item] },
    })));
    await expect(getApprovalTask(taskId)).rejects.toThrow("approval_response_invalid");
  });

  it("accepts approval item Decimal boundaries as unchanged strings", async () => {
    const item = {
      category: "it_equipment",
      name: "笔记本电脑",
      specification: null,
      quantity: "1.20",
      unit: "台",
      unit_price: "0.00",
      subtotal: "0",
    };
    mockedRequest.mockResolvedValueOnce(responseWith(detail({
      task: task({ subject: { ...task().subject, total: "1.2" } }),
      subject: {
        ...detail().subject,
        summary: { ...task().subject, total: "1.2" },
        items: [item],
      },
    })));
    const result = await getApprovalTask(taskId);
    expectSupported(result.task.subject);
    expectSupported(result.subject);
    expect(result.task.subject.total).toBe("1.2");
    expect(result.subject.items[0]).toMatchObject(item);
  });

  it("accepts each approval item Decimal precision maximum inclusively", async () => {
    const item = {
      category: "it_equipment",
      name: "笔记本电脑",
      specification: null,
      quantity: "9999999999.99",
      unit: "台",
      unit_price: "999999999999.99",
      subtotal: "99999999999999.99",
    };
    mockedRequest.mockResolvedValueOnce(responseWith(detail({
      subject: { ...detail().subject, items: [item] },
    })));

    const result = await getApprovalTask(taskId);
    expectSupported(result.subject);
    expect(result.subject.items[0]).toMatchObject(item);
  });

  it("reuses caller-owned operation UUIDs without generating IDs in approval requests", async () => {
    const approveOperation = "33333333-3333-4333-8333-333333333333";
    const rejectOperation = "44444444-4444-4444-8444-444444444444";
    const randomUuid = vi.spyOn(crypto, "randomUUID");
    mockedRequest
      .mockResolvedValueOnce(responseWith({
        instance_id: instanceId,
        status: "running",
        current_step_key: "procurement_review",
        replayed: false,
      }))
      .mockResolvedValueOnce(responseWith({
        instance_id: instanceId,
        status: "running",
        current_step_key: "procurement_review",
        replayed: true,
      }))
      .mockResolvedValueOnce(responseWith({
        instance_id: instanceId,
        status: "rejected",
        current_step_key: null,
        replayed: false,
      }));

    await approveTask(taskId, approveOperation, "同意");
    await approveTask(taskId, approveOperation, "同意");
    await rejectTask(taskId, "不符合要求", rejectOperation);

    expect(mockedRequest.mock.calls).toEqual([
      [`/approvals/tasks/${taskId}/approve`, {
        method: "POST",
        body: JSON.stringify({ client_operation_id: approveOperation, comment: "同意" }),
      }],
      [`/approvals/tasks/${taskId}/approve`, {
        method: "POST",
        body: JSON.stringify({ client_operation_id: approveOperation, comment: "同意" }),
      }],
      [`/approvals/tasks/${taskId}/reject`, {
        method: "POST",
        body: JSON.stringify({ client_operation_id: rejectOperation, reason: "不符合要求" }),
      }],
    ]);
    expect(randomUuid).not.toHaveBeenCalled();
  });

  it("preserves an explicit operation UUID when replaying an approval", async () => {
    const operationId = "55555555-5555-4555-8555-555555555555";
    mockedRequest.mockResolvedValueOnce(responseWith({
      instance_id: instanceId,
      status: "approved",
      current_step_key: null,
      replayed: true,
    }));
    const result = await approveTask(taskId, operationId, null);
    expect(result.replayed).toBe(true);
    expect(mockedRequest).toHaveBeenCalledWith(
      `/approvals/tasks/${taskId}/approve`,
      {
        method: "POST",
        body: JSON.stringify({ client_operation_id: operationId, comment: null }),
      },
    );
  });

  it("maps JSON decoding failures without swallowing request ApiError failures", async () => {
    mockedRequest.mockResolvedValueOnce({
      json: vi.fn().mockRejectedValue(new SyntaxError("invalid json")),
    } as unknown as Response);
    await expect(listApprovalTasks()).rejects.toThrow("approval_response_invalid");

    const apiError = new ApiError(503, "approval_unavailable");
    mockedRequest.mockRejectedValueOnce(apiError);
    await expect(listApprovalTasks()).rejects.toBe(apiError);
  });
});
