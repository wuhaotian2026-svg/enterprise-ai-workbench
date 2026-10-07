import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProcurementAssistantTurn } from "./ProcurementAssistantTurn";
import type { ProcurementTurnResponse } from "./types";


const turn: ProcurementTurnResponse = {
  id: "50000000-0000-4000-8000-000000000001",
  client_turn_id: "60000000-0000-4000-8000-000000000001",
  role: "assistant",
  request_content: "我想买三把单价500的椅子",
  text: "已记录椅子明细，请继续补充申请标题和用途。",
  blocks: [
    { type: "text", text: "已记录椅子明细，请继续补充申请标题和用途。" },
    {
      type: "assistant_draft",
      module_key: "procurement",
      intent: "draft_request",
      status: "active",
      version: 1,
      fields: {
        items: [{
          category_code: "office_supplies",
          item_name: "椅子",
          specification: null,
          quantity: "3",
          unit: "把",
          estimated_unit_price: "500",
        }],
      },
      pending_fields: [],
      missing_fields: ["title", "purpose", "needed_by_date", "currency"],
    },
  ],
  created_at: "2026-08-28T00:00:00Z",
  replayed: false,
};


describe("procurement assistant turn contract", () => {
  afterEach(cleanup);

  it("renders user request and validated assistant text in separate labelled regions", () => {
    render(<ProcurementAssistantTurn turn={turn} />);

    expect(screen.getByText("你提交的事项").parentElement).toHaveTextContent(
      "我想买三把单价500的椅子",
    );
    expect(screen.getByText("助手回复").parentElement).toHaveTextContent(
      "已记录椅子明细，请继续补充申请标题和用途。",
    );
  });

  it("shows the structured draft and exposes an explicit clear action", async () => {
    const onClearDraft = vi.fn();
    render(<ProcurementAssistantTurn turn={turn} onClearDraft={onClearDraft} />);

    expect(screen.getByLabelText("当前采购办事草稿")).toHaveTextContent("椅子 · 3 把 · 单价 ¥500");
    expect(screen.getByLabelText("当前采购办事草稿")).toHaveTextContent("申请标题、采购用途、需要日期、币种");
    await userEvent.click(screen.getByRole("button", { name: "清空草稿" }));
    expect(onClearDraft).toHaveBeenCalledTimes(1);
  });

  it("renders partial item leaf paths as actionable Chinese labels", () => {
    const partialTurn: ProcurementTurnResponse = {
      ...turn,
      text: "已记录标题、桌子、数量和单价，请只补充缺失信息。",
      blocks: [
        { type: "text", text: "已记录标题、桌子、数量和单价，请只补充缺失信息。" },
        {
          type: "assistant_draft",
          module_key: "procurement",
          intent: "draft_request",
          status: "active",
          version: 2,
          fields: { title: "办公用品" },
          pending_fields: ["items"],
          missing_fields: [
            "purpose",
            "items[0].unit",
            "items[0].category_code",
          ],
        },
      ],
    };

    render(<ProcurementAssistantTurn turn={partialTurn} />);

    const draft = screen.getByLabelText("当前采购办事草稿");
    expect(draft).toHaveTextContent("采购用途、第 1 项的计量单位、品类");
    expect(draft).not.toHaveTextContent("items[0].unit");
    expect(draft).not.toHaveTextContent("items[0].category_code");
  });

  it("renders whole item paths and fails closed for unknown internal paths", () => {
    const protectedTurn: ProcurementTurnResponse = {
      ...turn,
      text: "请补充采购信息。",
      blocks: [
        { type: "text", text: "请补充采购信息。" },
        {
          type: "assistant_draft",
          module_key: "procurement",
          intent: "draft_request",
          status: "active",
          version: 3,
          fields: { title: "办公用品" },
          pending_fields: [],
          missing_fields: [
            "items[0]",
            "legacy_procurement_pending",
            "items[x]",
            "items[0].future_leaf",
          ],
        },
      ],
    };

    render(<ProcurementAssistantTurn turn={protectedTurn} />);

    const draft = screen.getByLabelText("当前采购办事草稿");
    expect(draft).toHaveTextContent("第 1 项采购明细");
    expect(draft).toHaveTextContent(
      "草稿中存在无法识别的信息状态，请清空草稿后重试",
    );
    expect(draft).not.toHaveTextContent("待补充信息");
    expect(draft).not.toHaveTextContent("items[0]");
    expect(draft).not.toHaveTextContent("legacy_procurement_pending");
    expect(draft).not.toHaveTextContent("items[x]");
    expect(draft).not.toHaveTextContent("future_leaf");
  });

  it("renders a saved month-day as requiring only the needed-date year", () => {
    const yearTurn: ProcurementTurnResponse = {
      ...turn,
      text: "已记录需要日期，请只补充需要日期年份。",
      blocks: [
        { type: "text", text: "已记录需要日期，请只补充需要日期年份。" },
        {
          type: "clarification",
          missing_fields: ["needed_by_year"],
          suggestions: [],
        },
        {
          type: "assistant_draft",
          module_key: "procurement",
          intent: "draft_request",
          status: "active",
          version: 2,
          fields: { title: "办公用品", purpose: "办公室", currency: "CNY" },
          pending_fields: [],
          missing_fields: ["needed_by_year"],
        },
      ],
    };

    render(<ProcurementAssistantTurn turn={yearTurn} />);

    const draft = screen.getByLabelText("当前采购办事草稿");
    expect(draft).toHaveTextContent("还需补充：需要日期年份");
    expect(screen.queryByText("还需补充：需要日期", { exact: true })).toBeNull();
  });
});
