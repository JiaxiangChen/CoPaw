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

vi.mock("../../api/request", () => ({ request: vi.fn() }));

const mockRequest = vi.mocked(request);

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
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (path: string, options?: RequestInit) =>
          new Response(JSON.stringify(await mockRequest(path, options))),
      ),
    );
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
