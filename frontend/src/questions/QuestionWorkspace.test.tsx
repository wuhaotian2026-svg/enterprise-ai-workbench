import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QuestionWorkspace } from "./QuestionWorkspace";
import * as client from "../api/client";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, listQuestions: vi.fn(), getQuestion: vi.fn(), askQuestion: vi.fn(), submitFeedback: vi.fn(), deleteQuestion: vi.fn() };
});

afterEach(() => { cleanup(); vi.clearAllMocks(); });

const answered = {
  id: "q-1", text: "出差住宿标准是多少？", status: "completed", created_at: "2026-08-13T08:00:00Z",
  answer: { id: "a-1", status: "answered", text: "一线城市住宿上限为每晚 500 元。", refusal_reason: null,
    evidence_score: .91, citations: [{ number: 1, chunk_id: "c-1", evidence_snapshot: "一线城市住宿费每晚不超过500元。",
      document_name: "员工出差管理制度.pdf", mime_type: "application/pdf", page_number: 4,
      heading_path: "第三章 > 住宿标准", location: "page:4" }], clarification: null },
} satisfies client.QuestionDetail;

const clarification = {
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
    clarification: { questions: ["适用哪一城市档位？", "实际住宿几晚？"] },
  },
} satisfies client.QuestionDetail;

describe("question workspace", () => {
  it("shows clarification questions without submitting or rewriting the composer", async () => {
    vi.mocked(client.listQuestions).mockResolvedValue([]);
    vi.mocked(client.askQuestion).mockResolvedValue(clarification);
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />);
    const user = userEvent.setup();
    const textbox = await screen.findByLabelText("向制度知识库提问");
    await user.type(textbox, clarification.text);
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("适用哪一城市档位？")).toBeInTheDocument();
    expect(textbox).toHaveValue("");

    await user.type(textbox, "南京属于其他城市，住两晚");
    await user.click(screen.getByText("适用哪一城市档位？"));

    expect(textbox).toHaveValue("南京属于其他城市，住两晚");
    expect(client.askQuestion).toHaveBeenCalledTimes(1);
  });

  it("loads history, opens an answer, expands evidence, and updates feedback", async () => {
    vi.mocked(client.listQuestions).mockResolvedValue([{ id: "q-1", text: answered.text, status: "completed", created_at: answered.created_at }]);
    vi.mocked(client.getQuestion).mockResolvedValue(answered);
    vi.mocked(client.submitFeedback).mockResolvedValue({ id: "f-1", is_helpful: true, reason: null, updated_at: answered.created_at });
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /出差住宿标准/ }));
    expect(await screen.findByText("一线城市住宿上限为每晚 500 元。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /查看引用 1/ }));
    expect(screen.getByText("一线城市住宿费每晚不超过500元。")).toBeInTheDocument();
    expect(screen.getByText("员工出差管理制度.pdf")).toBeInTheDocument();
    expect(screen.getByText("第 4 页 · 第三章 > 住宿标准")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "有帮助" }));
    expect(await screen.findByText("反馈已记录")).toBeInTheDocument();
  });

  it("generates a question id before the first request and reuses it after an ambiguous failure", async () => {
    let rejectFirst!: (reason: unknown) => void;
    const pending = new Promise<client.QuestionDetail>((_resolve, reject) => { rejectFirst = reject; });
    vi.mocked(client.listQuestions).mockResolvedValue([]);
    vi.mocked(client.askQuestion).mockReturnValueOnce(pending).mockResolvedValueOnce(answered);
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />); const user = userEvent.setup();
    await user.type(await screen.findByLabelText("向制度知识库提问"), answered.text);
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(screen.getByRole("button", { name: "正在检索…" })).toBeDisabled();
    const firstQuestionId = vi.mocked(client.askQuestion).mock.calls[0][1];
    expect(firstQuestionId).toEqual(expect.any(String));
    rejectFirst(new client.ApiError(503, "model_timeout", { retryable: true, question_id: "server-id-must-not-replace-client-id" }));
    expect(await screen.findByText("模型暂时没有响应，问题已保留，可以安全重试。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试问题" }));
    await waitFor(() => expect(client.askQuestion).toHaveBeenLastCalledWith(answered.text, firstQuestionId));
    expect(await screen.findByText(answered.answer.text!)).toBeInTheDocument();
  });

  it("retains one question id for network retry and clears it after a deterministic error", async () => {
    vi.mocked(client.listQuestions).mockResolvedValue([]);
    vi.mocked(client.askQuestion)
      .mockRejectedValueOnce(new TypeError("network interrupted"))
      .mockResolvedValueOnce(answered)
      .mockRejectedValueOnce(new client.ApiError(422, "request_validation_failed"))
      .mockResolvedValueOnce(answered);
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />);
    const user = userEvent.setup();
    const textbox = await screen.findByLabelText("向制度知识库提问");

    await user.type(textbox, answered.text);
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    await user.click(await screen.findByRole("button", { name: "重试问题" }));
    await waitFor(() => expect(client.askQuestion).toHaveBeenCalledTimes(2));
    expect(vi.mocked(client.askQuestion).mock.calls[1][1])
      .toBe(vi.mocked(client.askQuestion).mock.calls[0][1]);

    await user.type(textbox, "这是确定性错误");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    await screen.findByText("暂时无法完成查询，请稍后再试。");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(client.askQuestion).toHaveBeenCalledTimes(4));
    expect(vi.mocked(client.askQuestion).mock.calls[3][1])
      .not.toBe(vi.mocked(client.askQuestion).mock.calls[2][1]);
  });

  it("prevents two synchronous submit events from starting duplicate questions", async () => {
    vi.mocked(client.listQuestions).mockResolvedValue([]);
    vi.mocked(client.askQuestion).mockReturnValue(new Promise(() => {}));
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />);
    const user = userEvent.setup();
    const textbox = await screen.findByLabelText("向制度知识库提问");
    await user.type(textbox, "同一事件周期只发送一次");
    const form = textbox.closest("form");
    expect(form).not.toBeNull();

    act(() => {
      form!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      form!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(client.askQuestion).toHaveBeenCalledTimes(1);
  });

  it("shows strict abstention and disables asking when the corpus is unavailable", async () => {
    vi.mocked(client.listQuestions).mockResolvedValue([]);
    vi.mocked(client.askQuestion).mockResolvedValue({ ...answered, answer: { ...answered.answer, status: "abstained", text: null,
      refusal_reason: "corpus_unavailable", evidence_score: 0, citations: [] } });
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />); const user = userEvent.setup();
    await user.type(await screen.findByLabelText("向制度知识库提问"), "年假制度是什么？");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("当前没有可用的制度资料，因此无法确认答案。")).toBeInTheDocument();
    expect(screen.getByLabelText("向制度知识库提问")).toBeDisabled();
  });

  it("shows delete only for the selected row and deletes without confirmation", async () => {
    const second = { ...answered, id: "q-2", text: "第二个问题" };
    vi.mocked(client.listQuestions).mockResolvedValue([
      { id: answered.id, text: answered.text, status: answered.status, created_at: answered.created_at },
      { id: second.id, text: second.text, status: second.status, created_at: second.created_at },
    ]);
    vi.mocked(client.getQuestion).mockResolvedValue(answered);
    vi.mocked(client.deleteQuestion).mockResolvedValue();
    const confirm = vi.spyOn(window, "confirm");
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />);
    const user = userEvent.setup();

    expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: new RegExp(answered.text) }));
    expect(screen.getAllByRole("button", { name: "删除" })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => expect(client.deleteQuestion).toHaveBeenCalledWith("q-1"));
    expect(confirm).not.toHaveBeenCalled();
    expect(screen.queryByText(answered.answer.text!)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: new RegExp(answered.text) })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: new RegExp(second.text) })).toBeInTheDocument();
  });

  it("disables delete while pending", async () => {
    let resolveDelete!: () => void;
    vi.mocked(client.listQuestions).mockResolvedValue([
      { id: answered.id, text: answered.text, status: answered.status, created_at: answered.created_at },
    ]);
    vi.mocked(client.getQuestion).mockResolvedValue(answered);
    vi.mocked(client.deleteQuestion).mockReturnValue(new Promise<void>((resolve) => { resolveDelete = resolve; }));
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: new RegExp(answered.text) }));
    await user.click(screen.getByRole("button", { name: "删除" }));

    expect(screen.getByRole("button", { name: "删除" })).toBeDisabled();
    resolveDelete();
    await waitFor(() => expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument());
  });

  it("retains the selected row and shows an inline error when deletion fails", async () => {
    vi.mocked(client.listQuestions).mockResolvedValue([
      { id: answered.id, text: answered.text, status: answered.status, created_at: answered.created_at },
    ]);
    vi.mocked(client.getQuestion).mockResolvedValue(answered);
    vi.mocked(client.deleteQuestion).mockRejectedValue(new client.ApiError(503, "request_failed"));
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: new RegExp(answered.text) }));
    await user.click(screen.getByRole("button", { name: "删除" }));

    const alert = await screen.findByRole("alert", { name: "删除历史记录失败" });
    expect(alert).toHaveTextContent("暂时无法删除这条历史记录，请稍后重试。");
    expect(screen.getByRole("button", { name: new RegExp(answered.text) })).toBeInTheDocument();
    expect(screen.getByText(answered.answer.text!)).toBeInTheDocument();
  });

  it("sends a non-empty question on Enter", async () => {
    vi.mocked(client.listQuestions).mockResolvedValue([]);
    vi.mocked(client.askQuestion).mockResolvedValue(answered);
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />);
    const user = userEvent.setup();
    const textbox = await screen.findByLabelText("向制度知识库提问");
    await user.type(textbox, "年假可以请几天？");

    fireEvent.keyDown(textbox, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(client.askQuestion).toHaveBeenCalledWith("年假可以请几天？", expect.any(String)));
  });

  it("does not send on Shift+Enter", async () => {
    vi.mocked(client.listQuestions).mockResolvedValue([]);
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />);
    const user = userEvent.setup();
    const textbox = await screen.findByLabelText("向制度知识库提问");
    await user.type(textbox, "第一行");

    fireEvent.keyDown(textbox, { key: "Enter", code: "Enter", shiftKey: true });

    expect(client.askQuestion).not.toHaveBeenCalled();
    expect(textbox).toHaveValue("第一行");
  });

  it("does not send Enter during IME composition", async () => {
    vi.mocked(client.listQuestions).mockResolvedValue([]);
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />);
    const user = userEvent.setup();
    const textbox = await screen.findByLabelText("向制度知识库提问");
    await user.type(textbox, "请假");

    fireEvent.keyDown(textbox, { key: "Enter", code: "Enter", isComposing: true });

    expect(client.askQuestion).not.toHaveBeenCalled();
  });

  it("shows the shared keyboard shortcut hint below the composer", async () => {
    vi.mocked(client.listQuestions).mockResolvedValue([]);
    render(<QuestionWorkspace username="employee" onLogout={vi.fn()} />);
    await screen.findByLabelText("向制度知识库提问");
    expect(screen.getByText("Enter 发送 · Shift+Enter 换行")).toBeInTheDocument();
  });
});
