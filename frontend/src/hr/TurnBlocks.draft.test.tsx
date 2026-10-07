import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TurnBlocks } from "./TurnBlocks";


describe("HR structured draft block", () => {
  afterEach(cleanup);

  it("renders explicit fields and lets the user clear the conversation draft", async () => {
    const clear = vi.fn();
    render(<TurnBlocks
      blocks={[{
        type: "assistant_draft",
        module_key: "hr",
        intent: "submit_leave",
        status: "active",
        version: 2,
        fields: { leave_type_code: "compensatory", reason: "为了就医" },
        pending_fields: ["date_range"],
        missing_fields: ["year"],
      }]}
      confirmationStates={{}}
      onSuggestion={() => undefined}
      onConfirm={() => undefined}
      onCancel={() => undefined}
      onRetry={() => undefined}
      onClearDraft={clear}
    />);

    expect(screen.getByLabelText("当前 HR 办事草稿")).toHaveTextContent("调休");
    expect(screen.getByLabelText("当前 HR 办事草稿")).toHaveTextContent("为了就医");
    expect(screen.getByLabelText("当前 HR 办事草稿")).toHaveTextContent("还需补充：请假年份");
    await userEvent.click(screen.getByRole("button", { name: "清空草稿" }));
    expect(clear).toHaveBeenCalledTimes(1);
  });

  it("maps date fields and filters persisted internal validator codes", () => {
    render(<TurnBlocks
      blocks={[
        {
          type: "clarification",
          missing_fields: ["date_range", "year"],
          suggestions: ["year:year_invalid", "2026年"],
        },
        {
          type: "assistant_draft",
          module_key: "hr",
          intent: "submit_leave",
          status: "active",
          version: 1,
          fields: { leave_type_code: "annual" },
          pending_fields: ["date_range"],
          missing_fields: ["year"],
        },
      ]}
      confirmationStates={{}}
      onSuggestion={() => undefined}
      onConfirm={() => undefined}
      onCancel={() => undefined}
      onRetry={() => undefined}
      onClearDraft={() => undefined}
    />);

    expect(screen.getByText("请假日期、请假年份")).toBeInTheDocument();
    expect(screen.getByText("待确认：请假日期")).toBeInTheDocument();
    expect(screen.queryByText("year:year_invalid")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "2026年" })).toBeInTheDocument();
  });

  it("hides unknown draft field identifiers behind user-facing labels", () => {
    const fieldSentinel = "legacy_hr_field";
    const pendingSentinel = "legacy_hr_pending";
    const missingSentinel = "legacy_hr_missing";
    render(<TurnBlocks
      blocks={[{
        type: "assistant_draft",
        module_key: "hr",
        intent: "submit_leave",
        status: "active",
        version: 1,
        fields: { [fieldSentinel]: "已验证值" },
        pending_fields: [pendingSentinel],
        missing_fields: [missingSentinel],
      }]}
      confirmationStates={{}}
      onSuggestion={() => undefined}
      onConfirm={() => undefined}
      onCancel={() => undefined}
      onRetry={() => undefined}
      onClearDraft={() => undefined}
    />);

    const draft = screen.getByLabelText("当前 HR 办事草稿");
    expect(draft).toHaveTextContent("已记录信息");
    expect(draft).toHaveTextContent("待确认：待确认信息");
    expect(draft).toHaveTextContent("还需补充：待补充信息");
    expect(draft).not.toHaveTextContent(fieldSentinel);
    expect(draft).not.toHaveTextContent(pendingSentinel);
    expect(draft).not.toHaveTextContent(missingSentinel);
  });
});
