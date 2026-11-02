import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "./api";
import {
  fetchDistributeBranches,
  fetchDistributeTargets,
} from "./distributeTargets";
import { useIframeStore } from "../../stores/iframeStore";

const auth = vi.hoisted(() => ({
  external: false,
  token: "initial",
  refresh: vi.fn(),
}));
vi.mock("../../api/authHeaders", () => ({
  buildAuthHeaders: () => ({
    "X-Source-Id": "RCZT",
    "X-Auth-Authorization": `Bearer ${auth.token}`,
  }),
}));
vi.mock("../../api/externalToken", () => ({
  isExternalTokenEnabled: () => auth.external,
  ensureValidToken: auth.refresh,
  clearExternalToken: vi.fn(),
}));

let previousEnv: typeof window.__env__;
beforeEach(() => {
  previousEnv = window.__env__;
  window.__env__ = { baseUrl: "https://gateway.example" };
  auth.external = false;
  auth.token = "initial";
  auth.refresh.mockReset();
  useIframeStore.getState().clearContext();
  useIframeStore
    .getState()
    .setContext({ userId: "0001", bbk: "100", positionId: "RB0101" });
  vi.stubGlobal("fetch", vi.fn());
});
afterEach(() => {
  window.__env__ = previousEnv;
  vi.unstubAllGlobals();
});

it.each([
  [
    "/api/agent/workspace/skill-config/list",
    () => api.fetchScenesByCategory("finance"),
    [],
  ],
  [
    "/api/agent/workspace/skill-stats",
    () => api.fetchSkillStats(["p1"]),
    { items: [] },
  ],
  [
    "/api/agent/workspace/name-list",
    () => api.fetchNameList([{ skillId: "s1" }]),
    { list: [] },
  ],
] as const)(
  "%s 通过统一 request 拼接 baseUrl 和鉴权",
  async (path, call, data) => {
    vi.mocked(fetch).mockResolvedValueOnce(Response.json({ code: 200, data }));
    await call();
    const [url, options] = vi.mocked(fetch).mock.calls[0];
    expect(new URL(String(url)).origin).toBe("https://gateway.example");
    expect(new URL(String(url)).pathname).toBe(path);
    expect(new Headers(options?.headers).get("X-Source-Id")).toBe("RCZT");
    expect(new Headers(options?.headers).get("X-Auth-Authorization")).toBe(
      "Bearer initial",
    );
  },
);

it("规划通过统一 request 拼接 API base 和 /api 前缀", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(
    Response.json({ code: 200, data: { items: [] } }),
  );
  await api.fetchPlanList(true);
  const [url, options] = vi.mocked(fetch).mock.calls[0];
  expect(String(url)).toMatch(
    /^https:\/\/gateway\.example\/api\/zt\/wealth\/plans\?/,
  );
  expect(new Headers(options?.headers).get("X-Source-Id")).toBe("RCZT");
});

it("规划 401 时复用统一 token 刷新和重试", async () => {
  auth.external = true;
  auth.refresh.mockImplementation(async () => {
    auth.token = "refreshed";
  });
  vi.mocked(fetch)
    .mockResolvedValueOnce(new Response(null, { status: 401 }))
    .mockResolvedValueOnce(Response.json({ code: 200, data: { items: [] } }));
  await expect(api.fetchPlanList(true)).resolves.toEqual([]);
  expect(auth.refresh).toHaveBeenCalledWith(true);
  expect(fetch).toHaveBeenCalledTimes(2);
  for (const [url] of vi.mocked(fetch).mock.calls) {
    expect(String(url)).toMatch(
      /^https:\/\/gateway\.example\/api\/zt\/wealth\/plans\?/,
    );
  }
  const retry = vi.mocked(fetch).mock.calls[1][1];
  expect(new Headers(retry?.headers).get("X-Auth-Authorization")).toBe(
    "Bearer refreshed",
  );
});

it("规划 200 HTML 响应仍按格式错误处理", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(
    new Response("<html>SPA</html>", {
      headers: { "Content-Type": "text/html" },
    }),
  );
  await expect(api.fetchPlanList(true)).rejects.toThrow("规划接口响应格式错误");
});

it.each([
  ["/api/zt/wealth/wmadm/branchTreeInfo", fetchDistributeBranches],
  ["/api/zt/wealth/wmadm/getUserPostionByBrn", fetchDistributeTargets],
] as const)("分发 %s 通过统一 request 拼接路径和鉴权", async (path, call) => {
  useIframeStore
    .getState()
    .setContext({ sysId: "retail", orgCode: "100001", orgLvl: "70" });
  vi.mocked(fetch).mockResolvedValueOnce(
    Response.json({ code: "200", data: [] }),
  );
  await expect(call()).resolves.toEqual([]);
  const [url, options] = vi.mocked(fetch).mock.calls[0];
  expect(String(url)).toBe(`https://gateway.example${path}`);
  expect(new Headers(options?.headers).get("X-Source-Id")).toBe("RCZT");
  expect(options?.signal).toBeInstanceOf(AbortSignal);
});
