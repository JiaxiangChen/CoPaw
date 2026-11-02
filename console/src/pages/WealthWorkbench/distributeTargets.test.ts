import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../../api/request";
import { useIframeStore } from "../../stores/iframeStore";
import {
  fetchDistributeBranches,
  fetchDistributeTargets,
} from "./distributeTargets";

vi.mock("../../api/request", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/request")>();
  return { ...actual, request: vi.fn(actual.request) };
});
vi.mock("../../api/config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
  clearAuthToken: vi.fn(),
}));
vi.mock("../../api/authHeaders", () => ({
  buildAuthHeaders: () => ({ "X-Source-Id": "RCZT" }),
}));
vi.mock("../../api/externalToken", () => ({
  isExternalTokenEnabled: () => false,
}));
const mockRequest =
  vi.fn<(path: string, options?: RequestInit) => Promise<unknown>>();

beforeEach(() => {
  mockRequest.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, options?: RequestInit) =>
      Response.json(await mockRequest(path, options)),
    ),
  );
  vi.mocked(request).mockClear();
  useIframeStore.getState().clearContext();
  useIframeStore.getState().setContext({
    bbk: "755",
    orgCode: "755480",
    orgLvl: "70",
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("机构岗位人员分发目标", () => {
  it("支行通过统一 request，读取 data 数组并附加统一鉴权", async () => {
    useIframeStore.getState().setContext({ sysId: "retail" });
    const branches = [
      { mOrgCode: "102", mOrgName: "某支行", mOrgLvl: 50, leaf: false },
    ];
    mockRequest.mockResolvedValueOnce({
      code: "200",
      message: "ok",
      data: branches,
      success: true,
    });
    expect(await fetchDistributeBranches()).toEqual(branches);
    expect(fetch).toHaveBeenCalledWith("/api/zt/wealth/wmadm/branchTreeInfo", {
      method: "POST",
      headers: expect.any(Headers),
      body: JSON.stringify({ mmOrgCode: "755", mmOrgLvl: 70, sysId: "retail" }),
      signal: expect.any(AbortSignal),
    });
    expect(request).toHaveBeenCalled();
    expect(
      new Headers(vi.mocked(fetch).mock.calls[0][1]?.headers).get(
        "X-Source-Id",
      ),
    ).toBe("RCZT");
  });

  it("支行缺少上下文时不查询，子层上下文无效仍返回 null", async () => {
    expect(await fetchDistributeBranches()).toEqual([]);
    expect(
      await fetchDistributeBranches({
        mOrgCode: "102",
        mOrgName: "某支行",
        mOrgLvl: 50,
        leaf: false,
      }),
    ).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([
    ["HTTP", () => new Response("error", { status: 502 })],
    ["JSON", () => new Response("invalid")],
  ])("%s 失败时支行可重试，人员显示空列表", async (_label, response) => {
    useIframeStore.getState().setContext({ sysId: "retail" });
    vi.mocked(fetch).mockImplementation(async () => response());
    expect(await fetchDistributeBranches()).toBeNull();
    expect(await fetchDistributeTargets()).toEqual([]);
  });

  it("60 秒超时中止两个接口并清理定时器", async () => {
    vi.useFakeTimers();
    useIframeStore.getState().setContext({ sysId: "retail" });
    vi.mocked(fetch).mockImplementation(
      (_path, options) =>
        new Promise((_resolve, reject) => {
          options?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    const branches = fetchDistributeBranches();
    const targets = fetchDistributeTargets();
    await vi.advanceTimersByTimeAsync(60000);
    expect(await branches).toBeNull();
    expect(await targets).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("按登录机构查询，使用 sapID 并保留返回的不同岗位", async () => {
    mockRequest.mockResolvedValue({
      code: "200",
      message: "ok",
      success: true,
      data: [
        {
          userID: "00080008",
          sapID: "00000000",
          userName: "邓XX",
          branchID: "00100",
          subBranchID: "001001",
          branchName: "XXX全行",
          subBranchName: null,
          postionId: "RB0101",
        },
        {
          sapID: "00000001",
          userName: "李XX",
          branchID: "00200",
          subBranchID: "002002",
          branchName: "XXX全行",
          subBranchName: "某支行",
          postionId: "RB1101",
        },
      ],
    });
    expect(await fetchDistributeTargets()).toEqual([
      {
        sapId: "00000000",
        name: "邓XX",
        orgName: "XXX全行",
        bbkId: "00100",
        bbkOrgId: "001001",
      },
      {
        sapId: "00000001",
        name: "李XX",
        orgName: "某支行",
        bbkId: "00200",
        bbkOrgId: "002002",
      },
    ]);
    expect(request).toHaveBeenCalled();
    expect(mockRequest).toHaveBeenCalledWith(
      "/api/zt/wealth/wmadm/getUserPostionByBrn",
      {
        method: "POST",
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
        body: JSON.stringify({
          bbkOrgId: "755",
          mmOrgCode: "755480",
          mmOrgLvl: 70,
        }),
      },
    );
  });

  it.each([null, "", " ", "NaN", "70.5"])(
    "机构级别 %s 无效时不发请求",
    async (orgLvl) => {
      useIframeStore.getState().setContext({ orgLvl });
      expect(await fetchDistributeTargets()).toEqual([]);
      expect(mockRequest).not.toHaveBeenCalled();
    },
  );

  it.each(["bbk", "orgCode"] as const)("缺少 %s 时不发请求", async (key) => {
    useIframeStore.getState().setContext({ [key]: null });
    expect(await fetchDistributeTargets()).toEqual([]);
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("业务失败和空列表返回空目标", async () => {
    mockRequest.mockResolvedValueOnce({ code: "failed", data: [] });
    expect(await fetchDistributeTargets()).toEqual([]);
    mockRequest.mockResolvedValueOnce({
      code: "200",
      message: "ok",
      success: true,
      data: [],
    });
    expect(await fetchDistributeTargets()).toEqual([]);
  });

  it.each([[[]], [null], [{}]])(
    "data 为 %j 时两个接口均按无数据处理",
    async (data) => {
      useIframeStore.getState().setContext({ sysId: "retail" });
      mockRequest.mockResolvedValue({
        code: "200",
        message: "ok",
        data,
        success: true,
      });
      expect(await fetchDistributeBranches()).toEqual([]);
      expect(await fetchDistributeTargets()).toEqual([]);
    },
  );

  it.each([false, undefined])(
    "success 为 %s 不影响 code=200 的有效列表",
    async (success) => {
      useIframeStore.getState().setContext({ sysId: "retail" });
      const branches = [
        { mOrgCode: "102", mOrgName: "某支行", mOrgLvl: 50, leaf: false },
      ];
      mockRequest.mockResolvedValueOnce({
        code: "200",
        success,
        data: branches,
      });
      expect(await fetchDistributeBranches()).toEqual(branches);
      mockRequest.mockResolvedValueOnce({
        code: "200",
        success,
        data: [
          {
            sapID: "0001",
            userName: "张三",
            branchName: "某分行",
            subBranchName: null,
          },
        ],
      });
      expect(await fetchDistributeTargets()).toEqual([
        { sapId: "0001", name: "张三", orgName: "某分行" },
      ]);
    },
  );

  it.each(
    [
      { code: "500", message: "failed", success: false, data: [] },
      { code: "500", message: "failed", success: true, data: [] },
      { code: "success", data: [] },
      [],
    ].map((payload) => [payload]),
  )("无效响应 %j 时支行保留重试，人员显示空列表", async (payload) => {
    useIframeStore.getState().setContext({ sysId: "retail" });
    mockRequest.mockResolvedValue(payload);
    expect(await fetchDistributeBranches()).toBeNull();
    expect(await fetchDistributeTargets()).toEqual([]);
  });

  it("请求失败时返回空目标", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    mockRequest.mockRejectedValue(new Error("offline"));
    expect(await fetchDistributeTargets()).toEqual([]);
    warn.mockRestore();
  });
});
