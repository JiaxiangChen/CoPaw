import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../../api/request";
import { useIframeStore } from "../../stores/iframeStore";
import {
  fetchDistributeBranches,
  fetchDistributeTargets,
} from "./distributeTargets";

vi.mock("../../api/request", () => ({ request: vi.fn() }));
const mockRequest =
  vi.fn<(path: string, options?: RequestInit) => Promise<unknown>>();

beforeEach(() => {
  mockRequest.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string, options?: RequestInit) =>
        new Response(JSON.stringify(await mockRequest(path, options))),
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
  it("支行直连网关路径，原样读取数组并使用默认同源 Cookie", async () => {
    useIframeStore.getState().setContext({ sysId: "retail" });
    const branches = [
      { mOrgCode: "102", mOrgName: "某支行", mOrgLvl: 50, leaf: false },
    ];
    mockRequest.mockResolvedValueOnce(branches);
    expect(await fetchDistributeBranches()).toEqual(branches);
    expect(fetch).toHaveBeenCalledWith(
      "/xcapi-auth/wmadm/services/v1/wmadm/combox/BranchTreeInfoService",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mOrgCode: "755", mOrgLvl: 70, sysId: "retail" }),
        signal: expect.any(AbortSignal),
      },
    );
    expect(request).not.toHaveBeenCalled();
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

  it("30 秒超时中止两个接口并清理定时器", async () => {
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
    await vi.advanceTimersByTimeAsync(30000);
    expect(await branches).toBeNull();
    expect(await targets).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("按登录机构查询，使用 sapID 并保留返回的不同岗位", async () => {
    mockRequest.mockResolvedValue({
      code: "success",
      errMsg: null,
      data: [
        {
          userID: "00080008",
          sapID: "00000000",
          userName: "邓XX",
          branchID: "100",
          branchName: "XXX全行",
          subBranchName: null,
          postionId: "RB0101",
        },
        {
          sapID: "00000001",
          userName: "李XX",
          branchName: "XXX全行",
          subBranchName: "某支行",
          postionId: "RB1101",
        },
      ],
    });
    expect(await fetchDistributeTargets()).toEqual([
      { sapId: "00000000", name: "邓XX", orgName: "XXX全行" },
      { sapId: "00000001", name: "李XX", orgName: "某支行" },
    ]);
    expect(request).not.toHaveBeenCalled();
    expect(mockRequest).toHaveBeenCalledWith(
      "/xcapi-auth/wmadm/services/v1/wmutadm/getUserPostionByBrn",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: expect.any(AbortSignal),
        body: JSON.stringify({
          bbkOrgId: "755",
          mOrgCode: "755480",
          mOrgLvl: 70,
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
    mockRequest.mockResolvedValueOnce({ code: "success", data: null });
    expect(await fetchDistributeTargets()).toEqual([]);
  });

  it("请求失败时返回空目标", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    mockRequest.mockRejectedValue(new Error("offline"));
    expect(await fetchDistributeTargets()).toEqual([]);
    warn.mockRestore();
  });
});
