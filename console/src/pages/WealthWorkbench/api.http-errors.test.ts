/**
 * 智能财富工作台 —— api.ts 错误传播测试
 * 规划写操作不做任何 mock 回退：HTTP 业务错误（4xx/5xx）、认证失效、
 * 网络不可达全部上抛，由调用方提示，避免「假成功」。
 * request 被文件级 mock，因此本文件独立于 api.test.ts。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import { request } from "../../api/request";
import type { Account, DistributeTarget } from "./types";

vi.mock("../../api/request", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/request")>();
  return { ...actual, request: vi.fn(actual.request) };
});
vi.mock("../../api/config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
  getApiToken: () => "",
  clearAuthToken: vi.fn(),
}));
vi.mock("../../api/externalToken", () => ({
  isExternalTokenEnabled: () => false,
  clearExternalToken: vi.fn(),
}));

const mockRequest = vi.mocked(request);
vi.mock("../../api/authHeaders", () => ({ buildAuthHeaders: () => ({}) }));

const RM_ACCOUNT: Account = {
  id: "rm",
  name: "张**",
  role: "客户经理",
  department: "xxx支行",
  source: "我的关注",
};

const RM_TARGET: DistributeTarget = {
  sapId: "rm",
  name: "张**",
  orgName: "xxx支行",
};

function httpError(status: number, message: string): Error {
  return Object.assign(new Error(message), { status });
}

async function publishWithDefaults() {
  return api.publishPlan(RM_ACCOUNT, api.newAccountDraft(), null, [RM_TARGET]);
}

describe("WealthWorkbench api 错误传播", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const actual = await vi.importActual<typeof import("../../api/request")>(
      "../../api/request",
    );
    mockRequest.mockImplementation(actual.request);
    vi.stubGlobal("fetch", vi.fn());
    api.resetMockDb();
  });

  it("publishPlan：后端返回 409 时上抛", async () => {
    mockRequest.mockRejectedValue(httpError(409, "规划发布中，请稍后再修改"));

    await expect(publishWithDefaults()).rejects.toMatchObject({
      status: 409,
    });
  });

  it("removePlan：后端返回 403 时上抛", async () => {
    mockRequest.mockRejectedValue(httpError(403, "无权限移除该规划"));

    await expect(api.removePlan("plan-1")).rejects.toMatchObject({
      status: 403,
    });
  });

  it.each([
    { status: 404, errorStatus: 404, payload: { detail: "plan not found" } },
    { status: 500, errorStatus: 500, payload: { detail: "plan not found" } },
    {
      status: 200,
      errorStatus: 404,
      payload: { code: 404, message: "plan not found" },
    },
    {
      status: 200,
      errorStatus: 500,
      payload: { code: 500, message: "plan not found" },
    },
  ])(
    "removePlan：规划不存在时提示刷新页面：%j",
    async ({ status, errorStatus, payload }) => {
      vi.mocked(fetch).mockResolvedValueOnce(
        jsonResponse(JSON.stringify(payload), { status }),
      );

      await expect(api.removePlan("plan-1")).rejects.toMatchObject({
        message: "该规划已被删除，请刷新页面后重试",
        status: errorStatus,
        data: payload,
      });
      expect(fetch).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    { status: 404, detail: "route not found" },
    { status: 403, detail: "无权限移除该规划" },
    { status: 409, detail: "规划发布中，请稍后再移除" },
  ])("removePlan：其他错误保留原文：%j", async ({ status, detail }) => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(JSON.stringify({ detail }), { status }),
    );

    await expect(api.removePlan("plan-1")).rejects.toMatchObject({
      message: detail,
      status,
    });
  });

  it("publishPlan：认证失效（无 status 的认证文案）时上抛", async () => {
    mockRequest.mockRejectedValue(
      new Error("认证已失效，请刷新页面或重新进入系统后再试"),
    );

    await expect(publishWithDefaults()).rejects.toThrow("认证已失效");
  });

  it("publishPlan：后端不可达（网络错误）时同样上抛，不回退 mock", async () => {
    mockRequest.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(publishWithDefaults()).rejects.toThrow("Failed to fetch");
  });

  it("removePlan：后端不可达（网络错误）时同样上抛，不回退 mock", async () => {
    mockRequest.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(api.removePlan("plan-1")).rejects.toThrow("Failed to fetch");
  });

  it("fetchPlanList：读路径接口不可达时返回空列表而非假数据", async () => {
    mockRequest.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(api.fetchPlanList()).resolves.toEqual([]);
  });
  it("看板严格刷新上抛网络错误，默认读取仍保持容错行为", async () => {
    mockRequest.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(api.fetchPlanList(true)).rejects.toThrow("Failed to fetch");
    await expect(api.fetchPlanList()).resolves.toEqual([]);
  });
});

afterEach(() => vi.unstubAllGlobals());

function jsonResponse(body: BodyInit | null, init: ResponseInit = {}) {
  return new Response(body, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
}
