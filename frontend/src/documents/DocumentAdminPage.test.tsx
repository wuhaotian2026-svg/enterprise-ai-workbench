import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DocumentAdminPage } from "./DocumentAdminPage";
import * as client from "../api/client";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, listDocuments: vi.fn(), listDocumentChunks: vi.fn(), uploadDocument: vi.fn(), disableDocument: vi.fn(), enableDocument: vi.fn(), reindexDocument: vi.fn() };
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const documents: client.PolicyDocument[] = [
  { id: "d1", display_name: "休假制度.pdf", mime_type: "application/pdf", status: "enabled", is_enabled: true, error_code: null, updated_at: "2026-08-13T12:00:00Z", chunk_count: 12 },
  { id: "d2", display_name: "采购规定.docx", mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", status: "processing", is_enabled: false, error_code: null, updated_at: "2026-08-13T12:00:00Z", chunk_count: 0 },
  { id: "d3", display_name: "差旅旧版.txt", mime_type: "text/plain", status: "disabled", is_enabled: false, error_code: null, updated_at: "2026-08-13T12:00:00Z", chunk_count: 8 },
  { id: "d4", display_name: "报销规则.pdf", mime_type: "application/pdf", status: "parse_failed", is_enabled: false, error_code: "pdf_parse_failed", updated_at: "2026-08-13T12:00:00Z", chunk_count: 0 },
  { id: "d5", display_name: "考勤制度.docx", mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", status: "index_failed", is_enabled: false, error_code: "embedding_timeout", updated_at: "2026-08-13T12:00:00Z", chunk_count: 0 },
];

describe("document administration", () => {
  it("shows supported upload types and all operational document states", async () => {
    vi.mocked(client.listDocuments).mockResolvedValue(documents);
    render(<DocumentAdminPage username="admin" onLogout={vi.fn()} />);
    expect(await screen.findByText("休假制度.pdf")).toBeInTheDocument();
    expect(screen.getByText("支持 PDF、DOCX、TXT，单个文件不超过系统限制。")).toBeInTheDocument();
    for (const status of ["已启用", "处理中", "已停用", "解析失败", "索引失败"]) expect(screen.getByText(status)).toBeInTheDocument();
    expect(screen.getByText("pdf_parse_failed")).toBeInTheDocument();
    expect(screen.getByText("2026/8/13 · 12")).toBeInTheDocument();
  });

  it("uploads a document with progress state and refreshes the list", async () => {
    let resolveUpload!: (value: client.PolicyDocument) => void;
    vi.mocked(client.listDocuments).mockResolvedValueOnce([]).mockResolvedValueOnce([documents[0]]);
    vi.mocked(client.uploadDocument).mockReturnValue(new Promise(resolve => { resolveUpload = resolve; }));
    render(<DocumentAdminPage username="admin" onLogout={vi.fn()} />); const user = userEvent.setup();
    const file = new File(["policy"], "policy.txt", { type: "text/plain" });
    await user.upload(await screen.findByLabelText("选择制度文件"), file);
    await user.click(screen.getByRole("button", { name: "上传并建立索引" }));
    expect(screen.getByRole("button", { name: "正在上传…" })).toBeDisabled();
    resolveUpload(documents[0]);
    await waitFor(() => expect(client.uploadDocument).toHaveBeenCalledWith(file));
    expect(await screen.findByText("休假制度.pdf")).toBeInTheDocument();
  });

  it("explains disable impact, performs no deletion, and can reindex failures", async () => {
    vi.mocked(client.listDocuments).mockResolvedValue(documents);
    vi.mocked(client.disableDocument).mockResolvedValue(); vi.mocked(client.reindexDocument).mockResolvedValue();
    render(<DocumentAdminPage username="admin" onLogout={vi.fn()} />); const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "停用 休假制度.pdf" }));
    expect(screen.getByText(/不会删除源文件或历史答案/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认停用" }));
    expect(client.disableDocument).toHaveBeenCalledWith("d1");
    await user.click(screen.getByRole("button", { name: "重新索引 报销规则.pdf" }));
    expect(client.reindexDocument).toHaveBeenCalledWith("d4");
  });

  it("opens a document detail with original-file actions and parsed chunks", async () => {
    vi.mocked(client.listDocuments).mockResolvedValue(documents);
    vi.mocked(client.listDocumentChunks).mockResolvedValue([{ id: "c1", sequence: 0, heading_path: "休假 > 年假", page_number: 2, location: "page:2", text: "员工每年享有五天年假。" }]);
    render(<DocumentAdminPage username="admin" onLogout={vi.fn()} />); const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "查看 休假制度.pdf" }));
    expect(await screen.findByRole("dialog", { name: "休假制度.pdf" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "在线预览" })).toHaveAttribute("href", "/api/v1/documents/d1/preview");
    expect(screen.getByRole("link", { name: "在线预览" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "下载原文件" })).toHaveAttribute("href", "/api/v1/documents/d1/content?download=true");
    expect(screen.getByText("休假 > 年假")).toBeInTheDocument();
    expect(screen.getByText("第 2 页")).toBeInTheDocument();
    expect(screen.getByText("员工每年享有五天年假。")).toBeInTheDocument();
  });

  it("keeps TXT parsed content visible without offering an unsupported preview", async () => {
    vi.mocked(client.listDocuments).mockResolvedValue(documents);
    vi.mocked(client.listDocumentChunks).mockResolvedValue([]);
    render(<DocumentAdminPage username="admin" onLogout={vi.fn()} />); const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "查看 差旅旧版.txt" }));
    expect(screen.queryByRole("link", { name: "在线预览" })).not.toBeInTheDocument();
    expect(screen.getByText("此格式请查看系统解析内容")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载原文件" })).toHaveAttribute("href", "/api/v1/documents/d3/content?download=true");
    expect(screen.getByRole("region", { name: "系统解析内容" })).toBeInTheDocument();
  });

  it("can re-enable a disabled document", async () => {
    vi.mocked(client.listDocuments).mockResolvedValue(documents);
    vi.mocked(client.enableDocument).mockResolvedValue();
    render(<DocumentAdminPage username="admin" onLogout={vi.fn()} />); const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "重新启用 差旅旧版.txt" }));
    expect(client.enableDocument).toHaveBeenCalledWith("d3");
  });

  it("shows an actionable Chinese upload error", async () => {
    vi.mocked(client.listDocuments).mockResolvedValue([]);
    vi.mocked(client.uploadDocument).mockRejectedValue(new client.ApiError(409, "duplicate_document"));
    render(<DocumentAdminPage username="admin" onLogout={vi.fn()} />); const user = userEvent.setup();
    await user.upload(await screen.findByLabelText("选择制度文件"), new File(["same"], "重复.txt", { type: "text/plain" }));
    await user.click(screen.getByRole("button", { name: "上传并建立索引" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("该文件内容已经上传，无需重复导入");
  });

  it("shows only a safe operation error and request id", async () => {
    vi.mocked(client.listDocuments).mockResolvedValue(documents);
    vi.mocked(client.reindexDocument).mockRejectedValue(new client.ApiError(409, "reindex_conflict", { request_id: "req-safe-01" }));
    render(<DocumentAdminPage username="admin" onLogout={vi.fn()} />); const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "重新索引 报销规则.pdf" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("reindex_conflict");
    expect(screen.getByRole("alert")).toHaveTextContent("req-safe-01");
  });

  it("polls while ingestion is active and stops after a stable state", async () => {
    vi.useFakeTimers();
    vi.mocked(client.listDocuments)
      .mockResolvedValueOnce([documents[1]])
      .mockResolvedValueOnce([documents[0]]);
    render(<DocumentAdminPage username="admin" onLogout={vi.fn()} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText("采购规定.docx")).toBeInTheDocument();

    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(client.listDocuments).toHaveBeenCalledTimes(2);
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });

    expect(client.listDocuments).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });
});
