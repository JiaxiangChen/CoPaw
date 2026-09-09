import { afterEach, describe, expect, it, vi } from "vitest";
import { monitorApi } from "./monitor";

vi.mock("../authHeaders", () => ({
  buildAuthHeaders: () => ({
    "X-Source-Id": "test-source",
    "X-Bbk-Id": "200",
    Authorization: "Bearer test",
  }),
}));
vi.mock("../config", () => ({ getApiUrl: (path: string) => `/api${path}` }));

const filters = { start_date: "2026-09-01", end_date: "2026-09-09" };
const excelType =
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

afterEach(() => vi.unstubAllGlobals());

describe("monitor branch dimension export API", () => {
  it("sends filters and paired sort parameters with auth headers and returns the server blob", async () => {
    const blob = new Blob(["server workbook"], { type: excelType });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers({ "Content-Type": excelType }),
      blob: async () => blob,
    });
    vi.stubGlobal("fetch", fetchMock);
    expect(
      await monitorApi.exportBranchDimension({
        ...filters,
        bbk_ids: "200,300",
        sort_by: "skillCount",
        sort_order: "desc",
      }),
    ).toBe(blob);
    const [url, options] = fetchMock.mock.calls[0];
    const parsed = new URL(url, "http://localhost");
    expect(parsed.pathname).toBe("/api/monitor/cron/export-branch-dimension");
    expect(Object.fromEntries(parsed.searchParams)).toEqual({
      ...filters,
      bbk_ids: "200,300",
      sort_by: "skillCount",
      sort_order: "desc",
    });
    expect(options.headers.get("Authorization")).toBe("Bearer test");
    expect(options.headers.get("X-Source-Id")).toBe("test-source");
    expect(options.headers.get("X-Bbk-Id")).toBe("200");
    expect(options.body).toBeUndefined();
  });

  it("omits optional filters for default ordering and all visible branches", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers({ "Content-Type": excelType }),
      blob: async () => new Blob(),
    });
    vi.stubGlobal("fetch", fetchMock);
    await monitorApi.exportBranchDimension(filters);
    expect(
      Object.fromEntries(
        new URL(fetchMock.mock.calls[0][0], "http://localhost").searchParams,
      ),
    ).toEqual(filters);
  });

  it.each([
    [403, { detail: "无权导出该分行" }, "无权导出该分行"],
    [422, { detail: [{ msg: "invalid date" }] }, "导出失败（HTTP 422）"],
    [500, null, "导出失败（HTTP 500）"],
  ])(
    "reports HTTP %s errors without downloading",
    async (status, error, message) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: false,
          status,
          json: async () => {
            if (error === null) throw new Error("not JSON");
            return error;
          },
        }),
      );
      await expect(monitorApi.exportBranchDimension(filters)).rejects.toThrow(
        message,
      );
    },
  );

  it("rejects a successful HTML or JSON response instead of saving a corrupt workbook", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        headers: new Headers({ "Content-Type": "text/html" }),
      }),
    );
    await expect(monitorApi.exportBranchDimension(filters)).rejects.toThrow(
      "导出接口未返回 Excel 文件",
    );
  });
});
