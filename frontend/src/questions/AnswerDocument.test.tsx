import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnswerDocument } from "./AnswerDocument";
import type { QuestionDetail } from "../api/client";

afterEach(cleanup);

describe("answer document", () => {
  it("renders a cited clarification as its own terminal state with feedback", async () => {
    const question = {
      id: "q-c",
      text: "南京出差三天多少钱？",
      status: "completed",
      created_at: "2026-08-18T00:00:00Z",
      answer: {
        id: "a-c",
        status: "needs_clarification",
        text: "其他城市住宿标准为350元/晚。",
        refusal_reason: null,
        evidence_score: 0.87,
        citations: [{
          number: 1,
          chunk_id: "c-c",
          evidence_snapshot: "其他城市住宿费每人每晚不超过350元。",
          source_status: "enabled",
          document_name: "差旅费用管理制度.txt",
          mime_type: "text/plain",
          page_number: null,
          heading_path: "住宿标准",
          location: "paragraph:8",
        }],
        clarification: {
          questions: ["适用哪一城市档位？", "实际住宿几晚？"],
        },
      },
    } satisfies QuestionDetail;
    const onFeedback = vi.fn().mockResolvedValue(undefined);
    render(<AnswerDocument question={question} onFeedback={onFeedback} />);
    const user = userEvent.setup();

    expect(screen.getByText("NEEDS CLARIFICATION / 需要补充信息")).toBeInTheDocument();
    expect(screen.getByText("其他城市住宿标准为350元/晚。")).toBeInTheDocument();
    expect(screen.getByText("适用哪一城市档位？")).toBeInTheDocument();
    expect(screen.getByText("实际住宿几晚？")).toBeInTheDocument();
    expect(screen.getByText("请在下方重新描述完整场景。")).toBeInTheDocument();
    expect(screen.queryByText("STRICT ABSTENTION")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /查看引用 1/ }));
    expect(screen.getByText("其他城市住宿费每人每晚不超过350元。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "有帮助" }));
    expect(onFeedback).toHaveBeenCalledWith("a-c", true);
  });

  it("renders a textual refusal reason instead of an unsupported answer", () => {
    const question = { id: "q", text: "未收录的问题", status: "completed", created_at: "2026-08-13T00:00:00Z",
      answer: { id: "a", status: "abstained", text: null, refusal_reason: "no_evidence", evidence_score: 0, citations: [], clarification: null } } satisfies QuestionDetail;
    render(<AnswerDocument question={question} onFeedback={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "无法从现有制度确认" })).toBeInTheDocument();
    expect(screen.getByText(/咨询人力资源或行政负责人/)).toBeInTheDocument();
  });

  it("keeps disabled-source evidence visible with an explicit warning", async () => {
    const question = { id: "q", text: "旧制度问题", status: "completed", created_at: "2026-08-13T00:00:00Z",
      answer: { id: "a", status: "answered", text: "历史回答。", refusal_reason: null, evidence_score: .8,
        citations: [{ number: 1, chunk_id: "c", evidence_snapshot: "该引用来源已停用，仅保留回答时的证据快照。", source_status: "disabled",
          document_name: "旧版制度.txt", mime_type: "text/plain", page_number: null, heading_path: null, location: "paragraph:1" }], clarification: null } } satisfies QuestionDetail;
    render(<AnswerDocument question={question} onFeedback={vi.fn()} />); const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /查看引用 1/ }));
    expect(screen.getByText("来源已停用；以下内容是回答生成时保存的证据快照。")).toBeInTheDocument();
  });
});
