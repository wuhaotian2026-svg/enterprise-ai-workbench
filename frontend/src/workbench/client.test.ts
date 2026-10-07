import { beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../api/request";
import { listWorkbenchModules } from "./client";

vi.mock("../api/request", () => ({ request: vi.fn() }));

const mockedRequest = vi.mocked(request);

function responseWith(body: unknown): Response {
  return {
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

describe("listWorkbenchModules", () => {
  beforeEach(() => {
    mockedRequest.mockReset();
  });

  it.each(["2026-08-16", "2026-08-23"] as const)(
    "accepts compatible catalog version %s",
    async (catalogVersion) => {
      mockedRequest.mockResolvedValueOnce(responseWith({
        catalog_version: catalogVersion,
        modules: [
          { key: "knowledge", label: "制度知识库", index: "01" },
          { key: "procurement", label: "采购申请", index: "08" },
        ],
      }));

      await expect(listWorkbenchModules()).resolves.toEqual({
        catalog_version: catalogVersion,
        modules: [
          { key: "knowledge", label: "制度知识库", index: "01" },
          { key: "procurement", label: "采购申请", index: "08" },
        ],
      });
      expect(mockedRequest).toHaveBeenCalledWith("/workbench/modules");
    },
  );

  it("rejects an unknown catalog version", async () => {
    mockedRequest.mockResolvedValueOnce(responseWith({
      catalog_version: "2026-09-01",
      modules: [],
    }));

    await expect(listWorkbenchModules()).rejects.toThrow(
      "workbench_module_response_invalid",
    );
  });
});
