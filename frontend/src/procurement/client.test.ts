import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, request } from "../api/request";
import {
  cancelProcurementConfirmation,
  confirmProcurementSubmission,
  createProcurementConversation,
  getProcurementRequest,
  getProcurementConversation,
  listProcurementConversations,
  listProcurementRequests,
  previewProcurementRequest,
  sendProcurementTurn,
  submitProcurementRequest,
  withdrawProcurementRequest,
} from "./client";

vi.mock("../api/request", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api/request")>(),
  request: vi.fn(),
}));

const mockedRequest = vi.mocked(request);
const requestId = "11111111-1111-4111-8111-111111111111";
const instanceId = "22222222-2222-4222-8222-222222222222";

function responseWith(body: unknown): Response {
  return { json: vi.fn().mockResolvedValue(body) } as unknown as Response;
}

function summary(overrides: Record<string, unknown> = {}) {
  return {
    id: requestId,
    request_number: "PR-2030-000001",
    title: "开发电脑",
    total: "19999.90",
    status: "pending_manager",
    submitted_at: "2030-01-02T03:04:05Z",
    ...overrides,
  };
}

function detail(overrides: Record<string, unknown> = {}) {
  return {
    id: requestId,
    summary: {
      request_number: "PR-2030-000001",
      title: "开发电脑",
      total: "19999.90",
      status: "pending_manager",
    },
    purpose: "研发使用",
    needed_by_date: "2030-02-03",
    currency: "CNY",
    items: [{
      category: "it_equipment",
      name: "笔记本电脑",
      specification: null,
      quantity: "2",
      unit: "台",
      unit_price: "9999.95",
      subtotal: "19999.90",
    }],
    applicant: { display_name: "测试员工" },
    organization: { display_name: "研发中心" },
    timeline: [{
      kind: "submitted",
      occurred_at: "2030-01-02T03:04:05+00:00",
      step_key: null,
      step_label: null,
      action: null,
      actor_display_name: "测试员工",
      comment: null,
      status: "submitted",
    }],
    ...overrides,
  };
}

describe("procurement client boundaries", () => {
  beforeEach(() => {
    mockedRequest.mockReset();
    vi.restoreAllMocks();
  });

  it("parses a closed request page while preserving canonical Decimal strings", async () => {
    mockedRequest.mockResolvedValueOnce(responseWith({
      items: [summary()], offset: 0, limit: 20, total: 1,
    }));

    const result = await listProcurementRequests({
      status: "pending_manager",
      submittedFrom: "2030-01-01",
      submittedTo: "2030-01-31",
      offset: 0,
      limit: 20,
    });

    expect(result.items[0]?.total).toBe("19999.90");
    expect(mockedRequest).toHaveBeenCalledWith(
      "/procurement/requests?status=pending_manager&submitted_from=2030-01-01&submitted_to=2030-01-31&offset=0&limit=20",
      undefined,
    );
  });

  it.each([
    ["non-string Decimal", summary({ total: 19.99 })],
    ["non-canonical Decimal", summary({ total: "019.99" })],
    ["illegal status", summary({ status: "draft" })],
    ["illegal UUID", summary({ id: "request-1" })],
    ["timezone-free datetime", summary({ submitted_at: "2030-01-02T03:04:05" })],
    ["extra response field", summary({ owner_user_id: requestId })],
    ["total with three decimal places", summary({ total: "19.999" })],
    ["total above the business maximum", summary({ total: "1000000000000.00" })],
  ])("rejects %s in list DTOs", async (_label, item) => {
    mockedRequest.mockResolvedValueOnce(responseWith({
      items: [item], offset: 0, limit: 20, total: 1,
    }));
    await expect(listProcurementRequests()).rejects.toThrow(
      "procurement_response_invalid",
    );
  });

  it.each(["0", "0.00", "1.2", "1.20", "999999999999.99"])(
    "preserves allowed canonical request total %s",
    async (total) => {
      mockedRequest.mockResolvedValueOnce(responseWith({
        items: [summary({ total })], offset: 0, limit: 20, total: 1,
      }));
      const page = await listProcurementRequests();
      expect(page.items[0]?.total).toBe(total);
    },
  );

  it("strictly parses detail dates, nested items, applicant and timeline", async () => {
    mockedRequest.mockResolvedValueOnce(responseWith(detail()));
    const result = await getProcurementRequest(requestId);
    expect(result.items[0]?.subtotal).toBe("19999.90");
    expect(result.needed_by_date).toBe("2030-02-03");
    expect(result.organization).toEqual({ display_name: "研发中心" });
    expect(mockedRequest).toHaveBeenCalledWith(
      `/procurement/requests/${requestId}`,
      undefined,
    );
  });

  it.each([
    ["impossible calendar date", detail({ needed_by_date: "2030-02-30" })],
    ["year-zero calendar date", detail({ needed_by_date: "0000-01-01" })],
    ["zero quantity", detail({ items: [{ ...detail().items[0], quantity: "0.00" }] })],
    ["illegal category", detail({ items: [{ ...detail().items[0], category: "cars" }] })],
    ["nested extra shape", detail({ applicant: { display_name: "员工", id: requestId } })],
    ["missing organization", (() => {
      const value = detail();
      const { organization: _organization, ...withoutOrganization } = value;
      return withoutOrganization;
    })()],
    ["extra organization shape", detail({ organization: { display_name: "研发中心", id: requestId } })],
    ["illegal organization display name", detail({ organization: { display_name: 42 } })],
    ["illegal timeline status", detail({ timeline: [{ ...detail().timeline[0], status: "draft" }] })],
    ["quantity with three decimal places", detail({ items: [{ ...detail().items[0], quantity: "1.234" }] })],
    ["quantity with eleven integer digits", detail({ items: [{ ...detail().items[0], quantity: "12345678901.00" }] })],
    ["unit price with three decimal places", detail({ items: [{ ...detail().items[0], unit_price: "1.234" }] })],
    ["unit price with thirteen integer digits", detail({ items: [{ ...detail().items[0], unit_price: "1234567890123.00" }] })],
    ["subtotal with three decimal places", detail({ items: [{ ...detail().items[0], subtotal: "1.234" }] })],
    ["subtotal with fifteen integer digits", detail({ items: [{ ...detail().items[0], subtotal: "123456789012345.00" }] })],
  ])("rejects %s in detail DTOs", async (_label, body) => {
    mockedRequest.mockResolvedValueOnce(responseWith(body));
    await expect(getProcurementRequest(requestId)).rejects.toThrow(
      "procurement_response_invalid",
    );
  });

  it("accepts field-specific Decimal edges without converting strings", async () => {
    mockedRequest.mockResolvedValueOnce(responseWith(detail({
      summary: { ...detail().summary, total: "1.20" },
      items: [{
        ...detail().items[0],
        quantity: "1.2",
        unit_price: "0",
        subtotal: "0.00",
      }],
    })));

    const result = await getProcurementRequest(requestId);
    expect(result.summary.total).toBe("1.20");
    expect(result.items[0]).toMatchObject({
      quantity: "1.2",
      unit_price: "0",
      subtotal: "0.00",
    });
  });

  it("accepts each inclusive Decimal precision maximum as an unchanged string", async () => {
    mockedRequest.mockResolvedValueOnce(responseWith(detail({
      items: [{
        ...detail().items[0],
        quantity: "9999999999.99",
        unit_price: "999999999999.99",
        subtotal: "99999999999999.99",
      }],
    })));

    const result = await getProcurementRequest(requestId);
    expect(result.items[0]).toMatchObject({
      quantity: "9999999999.99",
      unit_price: "999999999999.99",
      subtotal: "99999999999999.99",
    });
  });

  it("reuses a caller-owned submit operation UUID without generating network IDs", async () => {
    const operationId = "33333333-3333-4333-8333-333333333333";
    const randomUuid = vi.spyOn(crypto, "randomUUID");
    mockedRequest
      .mockResolvedValueOnce(responseWith({ ...summary(), replayed: false }))
      .mockResolvedValueOnce(responseWith({ ...summary(), replayed: false }));
    const input = {
      title: "开发电脑",
      purpose: "研发使用",
      needed_by_date: "2030-02-03",
      currency: "CNY" as const,
      items: [{
        category_code: "it_equipment" as const,
        item_name: "笔记本电脑",
        specification: null,
        quantity: "2",
        unit: "台",
        estimated_unit_price: "9999.95",
      }],
    };

    await submitProcurementRequest(input, operationId);
    await submitProcurementRequest(input, operationId);

    const bodies = mockedRequest.mock.calls.map((call) => JSON.parse(
      String(call[1]?.body),
    ) as Record<string, unknown>);
    expect(bodies.map((body) => body.client_operation_id)).toEqual([
      operationId,
      operationId,
    ]);
    expect(bodies[0]).toEqual({ ...input, client_operation_id: operationId });
    expect(randomUuid).not.toHaveBeenCalled();
  });

  it("previews authoritative totals without sending an operation ID", async () => {
    const input = {
      title: "开发耗材",
      purpose: "研发使用",
      needed_by_date: "2030-02-03",
      currency: "CNY" as const,
      items: [
        {
          category_code: "office_supplies" as const,
          item_name: "签字笔",
          specification: null,
          quantity: "2",
          unit: "盒",
          estimated_unit_price: "10.00",
        },
        {
          category_code: "other" as const,
          item_name: "测试耗材",
          specification: null,
          quantity: "0.33",
          unit: "件",
          estimated_unit_price: "0.05",
        },
      ],
    };
    mockedRequest.mockResolvedValueOnce(responseWith({
      currency: "CNY",
      subtotals: ["20.00", "0.02"],
      total: "20.02",
    }));

    await expect(previewProcurementRequest(input)).resolves.toEqual({
      currency: "CNY",
      subtotals: ["20.00", "0.02"],
      total: "20.02",
    });
    expect(mockedRequest).toHaveBeenCalledWith(
      "/procurement/requests/preview",
      { method: "POST", body: JSON.stringify(input) },
    );
    expect(JSON.parse(String(mockedRequest.mock.calls[0]?.[1]?.body))).not.toHaveProperty(
      "client_operation_id",
    );
  });

  it.each([
    ["numeric subtotal", { currency: "CNY", subtotals: [20], total: "20.00" }],
    ["non-canonical subtotal", { currency: "CNY", subtotals: ["20"], total: "20.00" }],
    ["non-canonical total", { currency: "CNY", subtotals: ["20.00"], total: "20.0" }],
    ["wrong currency", { currency: "USD", subtotals: ["20.00"], total: "20.00" }],
    ["extra response field", { currency: "CNY", subtotals: ["20.00"], total: "20.00", internal: true }],
    ["wrong subtotal count", { currency: "CNY", subtotals: [], total: "20.00" }],
    ["subtotal above the business maximum", { currency: "CNY", subtotals: ["1000000000000.00"], total: "20.00" }],
    ["subtotal above Decimal field maximum", { currency: "CNY", subtotals: ["100000000000000.00"], total: "20.00" }],
    ["total above business maximum", { currency: "CNY", subtotals: ["20.00"], total: "1000000000000.00" }],
  ])("rejects %s in preview responses", async (_label, body) => {
    mockedRequest.mockResolvedValueOnce(responseWith(body));
    await expect(previewProcurementRequest({
      title: "开发耗材",
      purpose: "研发使用",
      needed_by_date: "2030-02-03",
      currency: "CNY",
      items: [{
        category_code: "office_supplies",
        item_name: "签字笔",
        specification: null,
        quantity: "2",
        unit: "盒",
        estimated_unit_price: "10.00",
      }],
    })).rejects.toThrow("procurement_response_invalid");
  });

  it.each([0, 51])(
    "rejects preview parsing when the request contains %i items",
    async (itemCount) => {
      const item = {
        category_code: "office_supplies" as const,
        item_name: "签字笔",
        specification: null,
        quantity: "1",
        unit: "盒",
        estimated_unit_price: "10.00",
      };
      mockedRequest.mockResolvedValueOnce(responseWith({
        currency: "CNY",
        subtotals: Array.from({ length: itemCount }, () => "10.00"),
        total: "10.00",
      }));

      await expect(previewProcurementRequest({
        title: "开发耗材",
        purpose: "研发使用",
        needed_by_date: "2030-02-03",
        currency: "CNY",
        items: Array.from({ length: itemCount }, () => ({ ...item })),
      })).rejects.toThrow("procurement_response_invalid");
    },
  );

  it("passes AbortSignal through the preview read", async () => {
    const controller = new AbortController();
    const input = {
      title: "开发耗材",
      purpose: "研发使用",
      needed_by_date: "2030-02-03",
      currency: "CNY" as const,
      items: [{
        category_code: "office_supplies" as const,
        item_name: "签字笔",
        specification: null,
        quantity: "2",
        unit: "盒",
        estimated_unit_price: "10.00",
      }],
    };
    mockedRequest.mockResolvedValueOnce(responseWith({
      currency: "CNY", subtotals: ["20.00"], total: "20.00",
    }));

    await previewProcurementRequest(input, controller.signal);

    expect(mockedRequest).toHaveBeenCalledWith(
      "/procurement/requests/preview",
      { method: "POST", body: JSON.stringify(input), signal: controller.signal },
    );
  });

  it("requires caller-owned write and turn IDs at the TypeScript boundary", () => {
    const input = {
      title: "开发电脑",
      purpose: "研发使用",
      needed_by_date: "2030-02-03",
      currency: "CNY" as const,
      items: [{
        category_code: "it_equipment" as const,
        item_name: "笔记本电脑",
        specification: null,
        quantity: "2",
        unit: "台",
        estimated_unit_price: "9999.95",
      }],
    };
    if (false) {
      // @ts-expect-error Submission intents must provide and retain their own operation ID.
      submitProcurementRequest(input);
      // @ts-expect-error Withdrawal intents must provide and retain their own operation ID.
      withdrawProcurementRequest(requestId);
      // @ts-expect-error Turn intents must provide and retain their own turn ID.
      sendProcurementTurn(requestId, "帮我申请电脑");
      // @ts-expect-error Confirmation intents must provide and retain their own operation ID.
      confirmProcurementSubmission(requestId);
    }
    expect(true).toBe(true);
  });

  it("preserves an explicit operation UUID for an intentional replay", async () => {
    const operationId = "55555555-5555-4555-8555-555555555555";
    mockedRequest.mockResolvedValueOnce(responseWith({
      instance_id: instanceId,
      status: "cancelled",
      current_step_key: null,
      replayed: true,
    }));

    await expect(withdrawProcurementRequest(requestId, operationId)).resolves.toEqual({
      instance_id: instanceId,
      status: "cancelled",
      current_step_key: null,
      replayed: true,
    });
    expect(mockedRequest).toHaveBeenCalledWith(
      `/procurement/requests/${requestId}/withdraw`,
      {
        method: "POST",
        body: JSON.stringify({ client_operation_id: operationId }),
      },
    );
  });

  it("strictly parses conversation and turn DTOs", async () => {
    const conversationId = "66666666-6666-4666-8666-666666666666";
    const turnId = "77777777-7777-4777-8777-777777777777";
    const now = "2030-01-02T03:04:05Z";
    mockedRequest
      .mockResolvedValueOnce(responseWith({
        id: conversationId, title: "采购助手", created_at: now, updated_at: now,
      }))
      .mockResolvedValueOnce(responseWith([{
        id: conversationId, title: "采购助手", created_at: now, updated_at: now,
      }]))
      .mockResolvedValueOnce(responseWith({
        id: conversationId,
        title: "采购助手",
        created_at: now,
        updated_at: now,
        turns: [{
          id: turnId,
          client_turn_id: turnId,
          role: "assistant",
          request_content: "我想采购椅子",
          text: "已查询",
          blocks: [{ type: "text", text: "已查询" }],
          created_at: now,
        }],
      }));

    await expect(createProcurementConversation("采购助手")).resolves.toMatchObject({
      id: conversationId,
      title: "采购助手",
    });
    await expect(listProcurementConversations()).resolves.toHaveLength(1);
    const conversation = await getProcurementConversation(conversationId);
    expect(conversation.turns[0]?.request_content).toBe("我想采购椅子");
    expect(conversation.turns[0]?.blocks).toEqual([{ type: "text", text: "已查询" }]);
  });

  it("passes AbortSignal through conversation list and detail reads", async () => {
    const conversationId = "66666666-6666-4666-8666-666666666666";
    const now = "2030-01-02T03:04:05Z";
    const listController = new AbortController();
    const detailController = new AbortController();
    mockedRequest
      .mockResolvedValueOnce(responseWith([]))
      .mockResolvedValueOnce(responseWith({
        id: conversationId,
        title: "采购助手",
        created_at: now,
        updated_at: now,
        turns: [],
      }));

    await listProcurementConversations(listController.signal);
    await getProcurementConversation(conversationId, detailController.signal);

    expect(mockedRequest.mock.calls).toEqual([
      ["/procurement/conversations", { signal: listController.signal }],
      [`/procurement/conversations/${conversationId}`, { signal: detailController.signal }],
    ]);
  });

  it.each([
    ["unknown block", { type: "server_extension", payload: "hidden" }],
    ["extra text field", { type: "text", text: "回答", raw_prompt: "hidden" }],
  ])("rejects %s in a stored turn", async (_label, block) => {
    const conversationId = "66666666-6666-4666-8666-666666666666";
    const turnId = "77777777-7777-4777-8777-777777777777";
    const now = "2030-01-02T03:04:05Z";
    mockedRequest.mockResolvedValueOnce(responseWith({
      id: conversationId,
      title: "采购助手",
      created_at: now,
      updated_at: now,
      turns: [{
        id: turnId,
        client_turn_id: turnId,
        role: "assistant",
        text: "回答",
        blocks: [block],
        created_at: now,
      }],
    }));
    await expect(getProcurementConversation(conversationId)).rejects.toThrow(
      "procurement_response_invalid",
    );
  });

  it("reuses caller-owned turn and confirmation IDs without implicit generation", async () => {
    const conversationId = "66666666-6666-4666-8666-666666666666";
    const confirmationId = "77777777-7777-4777-8777-777777777777";
    const turnId = "88888888-8888-4888-8888-888888888888";
    const confirmationOperation = "99999999-9999-4999-8999-999999999999";
    const now = "2030-01-02T03:04:05Z";
    const randomUuid = vi.spyOn(crypto, "randomUUID");
    mockedRequest
      .mockResolvedValueOnce(responseWith({
        id: turnId,
        client_turn_id: turnId,
        role: "assistant",
        request_content: "帮我申请电脑",
        text: "请确认",
        blocks: [],
        created_at: now,
        replayed: false,
      }))
      .mockResolvedValueOnce(responseWith({
        id: turnId,
        client_turn_id: turnId,
        role: "assistant",
        request_content: "帮我申请电脑",
        text: "请确认",
        blocks: [],
        created_at: now,
        replayed: true,
      }))
      .mockResolvedValueOnce(responseWith({
        type: "execution_result",
        resource_type: "procurement_request",
        resource_id: requestId,
        replayed: false,
      }))
      .mockResolvedValueOnce(responseWith({
        type: "execution_result",
        resource_type: "procurement_request",
        resource_id: requestId,
        replayed: true,
      }))
      .mockResolvedValueOnce(responseWith({
        confirmation_id: confirmationId,
        status: "cancelled",
      }));

    await sendProcurementTurn(conversationId, "帮我申请电脑", turnId);
    await sendProcurementTurn(conversationId, "帮我申请电脑", turnId);
    await confirmProcurementSubmission(confirmationId, confirmationOperation);
    await confirmProcurementSubmission(confirmationId, confirmationOperation);
    await cancelProcurementConfirmation(confirmationId);

    expect(JSON.parse(String(mockedRequest.mock.calls[0]?.[1]?.body))).toMatchObject({
      client_turn_id: turnId,
    });
    expect(JSON.parse(String(mockedRequest.mock.calls[1]?.[1]?.body))).toMatchObject({
      client_turn_id: turnId,
    });
    expect(JSON.parse(String(mockedRequest.mock.calls[2]?.[1]?.body))).toEqual({
      client_operation_id: confirmationOperation,
    });
    expect(JSON.parse(String(mockedRequest.mock.calls[3]?.[1]?.body))).toEqual({
      client_operation_id: confirmationOperation,
    });
    expect(mockedRequest.mock.calls[4]?.[1]?.body).toBe("{}");
    expect(randomUuid).not.toHaveBeenCalled();
  });

  it("maps JSON decoding failures without swallowing request ApiError failures", async () => {
    mockedRequest.mockResolvedValueOnce({
      json: vi.fn().mockRejectedValue(new SyntaxError("invalid json")),
    } as unknown as Response);
    await expect(listProcurementRequests()).rejects.toThrow("procurement_response_invalid");

    const apiError = new ApiError(503, "procurement_unavailable");
    mockedRequest.mockRejectedValueOnce(apiError);
    await expect(listProcurementRequests()).rejects.toBe(apiError);
  });
});
