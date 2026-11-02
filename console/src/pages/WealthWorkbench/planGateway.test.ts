import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import { request } from "../../api/request";
import { useIframeStore } from "../../stores/iframeStore";
import { useAgentStore } from "../../stores/agentStore";
import type { Account, Draft } from "./types";

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

vi.mock("../../api/authHeaders", () => ({
  buildAuthHeaders: () => ({ "X-Source-Id": "RCZT", "X-User-Id": "000123" }),
}));

const account: Account = {
  id: "president",
  name: "行长",
  role: "支行行长",
  department: "支行",
  source: "行长关注",
};
const draft: Draft = {
  name: "  经营规划  ",
  items: [
    {
      id: "skill-a",
      sceneName: "经营场景",
      categoryCode: "finance",
      categoryLabel: "理财",
      itemId: "item-a",
      direction: "方向",
      cycle: "自定义",
      start: "2026-10-19",
      end: "2026-10-31",
      cronExample: "生成名单",
      mcpRelations: ["mcp-a"],
      schedule: { type: "daily", hour: 9, minute: 0 },
    },
  ],
};
const view = {
  id: "plan-a",
  name: "经营规划",
  sourceLabel: "行长关注",
  periodStart: "2026-10-19",
  periodEnd: "2026-10-31",
  publishStatus: "published",
  boardStatus: "分发中",
  editable: true,
  scenes: [
    {
      sceneId: "skill-a",
      sceneName: "经营场景",
      category: "finance",
      categoryLabel: "理财",
      itemId: "item-a",
      cronExpr: "0 9 * * *",
      cronExample: "生成名单",
      mcpRelations: ["mcp-a"],
      startDate: "2026-10-19",
      endDate: "2026-10-31",
    },
  ],
  targets: [
    {
      sapId: "0001",
      name: "客户经理",
      bbkId: "recipient-bank",
      bbkOrgId: "recipient-org",
    },
  ],
};
const ok = (data: unknown) =>
  jsonResponse(JSON.stringify({ code: "200", message: "ok", data }));

beforeEach(() => {
  vi.mocked(request).mockClear();
  vi.stubGlobal("fetch", vi.fn());
  useIframeStore.getState().clearContext();
  useIframeStore.getState().setContext({
    userId: "000123",
    positionId: "RB1101",
    bbk: "branch-a",
    orgCode: "org-a",
    userName: "真实姓名",
    source: "RMASSIST",
  });
  useAgentStore.getState().setSelectedAgent("agent-a");
  document.cookie = "token=gateway-token; path=/";
});
afterEach(() => {
  vi.unstubAllGlobals();
  useAgentStore.getState().setSelectedAgent("default");
  document.cookie = "token=; Max-Age=0; path=/";
});

describe("Java 规划网关契约", () => {
  it.each(["daily", "weekly"] as const)(
    "%s 名单推送未设置时间时提交08:00，督办仍为11:00",
    async (type) => {
      vi.mocked(fetch)
        .mockResolvedValueOnce(ok({ id: "plan-a" }))
        .mockResolvedValueOnce(ok({ items: [view] }));
      await api.publishPlan(
        account,
        {
          ...draft,
          items: [{ ...draft.items[0], schedule: { type } }],
        },
        null,
        [],
      );
      const body = JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body));
      expect(body.scenes[0]).toMatchObject({
        cronExpr: type === "daily" ? "0 8 * * *" : "0 8 * * mon",
        monitorTimeCron: "0 11 * * *",
      });
    },
  );

  it.each([
    [null, "RCZT"],
    ["plan-a", "RCZT"],
    [null, "NEW_CHANNEL"],
    ["plan-a", "NEW_CHANNEL"],
  ] as const)(
    "创建/更新规划 %s 时保留消息渠道 %s",
    async (editingId, source) => {
      useIframeStore.getState().setContext({ source });
      vi.mocked(fetch)
        .mockResolvedValueOnce(
          ok({ id: "plan-a", publishStatus: "publishing" }),
        )
        .mockResolvedValueOnce(ok({ items: [view] }));
      await api.publishPlan(account, draft, editingId, []);
      expect(
        JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body)),
      ).toMatchObject({ sourceId: source, sapId: "000123" });
    },
  );
  it("列表在同源发送查询身份，解包并映射驼峰模型", async () => {
    vi.mocked(fetch).mockResolvedValue(ok({ items: [view] }));
    const plans = await api.fetchPlanList(true);
    const [path, options] = vi.mocked(fetch).mock.calls[0];
    const url = new URL(String(path), "https://gateway.example");
    expect(url.pathname).toBe("/api/zt/wealth/plans");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      sapId: "000123",
      role: "president",
      bbkId: "branch-a",
    });
    expect(new Headers(options?.headers).get("X-Source-Id")).toBe("RCZT");
    expect(new Headers(options?.headers).get("X-User-Id")).toBe("000123");
    expect(options).not.toHaveProperty("body");
    expect(request).toHaveBeenCalled();
    expect(plans[0]).toMatchObject({
      source: "行长关注",
      publishStatus: "published",
      status: "分发中",
      targetSapIds: ["0001"],
      targets: [
        {
          sapId: "0001",
          name: "客户经理",
          orgName: "",
          bbkId: "recipient-bank",
          bbkOrgId: "recipient-org",
        },
      ],
      items: [
        { id: "skill-a", cronExample: "生成名单", mcpRelations: ["mcp-a"] },
      ],
    });
  });

  it.each([null, "plan/a"])(
    "创建/更新 %s 使用 JSON 身份和驼峰字段",
    async (editingId) => {
      vi.mocked(fetch)
        .mockResolvedValueOnce(
          ok({ id: "plan-a", publishStatus: "publishing" }),
        )
        .mockResolvedValueOnce(ok({ items: [view] }));
      await api.publishPlan(account, draft, editingId, [
        {
          sapId: "0001",
          name: "客户经理",
          orgName: "支行",
          bbkId: "recipient-bank",
          bbkOrgId: "recipient-org",
        },
      ]);
      const [path, options] = vi.mocked(fetch).mock.calls[0];
      expect(path).toBe(
        editingId ? "/api/zt/wealth/plans/plan%2Fa" : "/api/zt/wealth/plans",
      );
      expect(options?.method).toBe(editingId ? "PUT" : "POST");
      expect(JSON.parse(String(options?.body))).toEqual({
        sapId: "000123",
        role: "president",
        bbkId: "branch-a",
        bbkOrgId: "org-a",
        creatorName: "真实姓名",
        sourceId: "RMASSIST",
        agentId: "agent-a",
        name: "经营规划",
        sourceLabel: "行长关注",
        scenes: [
          {
            sceneId: "skill-a",
            sceneName: "经营场景",
            category: "finance",
            itemId: "item-a",
            direction: "方向",
            cycle: "自定义",
            startDate: "2026-10-19",
            endDate: "2026-10-31",
            cronExpr: "0 9 * * *",
            schedule: 1,
            monitorTimeCron: "0 11 * * *",
            finishRate: 60,
            cronExample: "生成名单",
            mcpRelations: ["mcp-a"],
          },
        ],
        targets: [
          {
            sapId: "0001",
            name: "客户经理",
            bbkId: "recipient-bank",
            bbkOrgId: "recipient-org",
          },
        ],
      });
      expect(request).toHaveBeenCalled();
    },
  );

  it("删除传 DELETE JSON 身份，成功后重新查列表", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(ok(true))
      .mockResolvedValueOnce(ok({ items: [] }));
    await api.removePlan("plan/a");
    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/api/zt/wealth/plans/plan%2Fa",
      expect.objectContaining({
        method: "DELETE",
        body: JSON.stringify({ sapId: "000123" }),
      }),
    );
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it.each(["400", "403", "404", "409"])(
    "HTTP 200 业务码 %s 写操作抛错且不刷新",
    async (code) => {
      vi.mocked(fetch).mockResolvedValue(
        jsonResponse(JSON.stringify({ code, message: "业务失败", data: null })),
      );
      await expect(
        api.publishPlan(account, draft, null, []),
      ).rejects.toMatchObject({ message: "业务失败", status: Number(code) });
      expect(fetch).toHaveBeenCalledTimes(1);
    },
  );

  it("读取业务失败时严格刷新上抛，普通读取降级", async () => {
    vi.mocked(fetch).mockImplementation(async () =>
      jsonResponse(
        JSON.stringify({ code: 403, message: "不可见", data: null }),
      ),
    );
    await expect(api.fetchPlanList(true)).rejects.toMatchObject({
      status: 403,
    });
    await expect(api.fetchPlanList()).resolves.toEqual([]);
  });

  it("HTTP 失败保留后端文案，成功空数据不能当作完成", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        jsonResponse(JSON.stringify({ message: "网关不可用" }), {
          status: 502,
        }),
      )
      .mockResolvedValueOnce(ok(null));
    await expect(api.removePlan("a")).rejects.toMatchObject({
      status: 502,
      message: "网关不可用",
    });
    await expect(api.removePlan("a")).rejects.toThrow();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("取消信号透传，取消后的晚响应不返回旧列表", async () => {
    const controller = new AbortController();
    vi.mocked(fetch).mockImplementation(async (_url, options) => {
      expect(options?.signal).toBe(controller.signal);
      controller.abort();
      return ok({ items: [view] });
    });
    await expect(
      api.fetchPlanList(true, controller.signal),
    ).rejects.toMatchObject({ name: "AbortError" });
  });

  it("网关非 JSON 错误保留 HTTP 状态，删除 false 不作成功处理", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        jsonResponse("<html>Bad Gateway</html>", { status: 502 }),
      )
      .mockResolvedValueOnce(ok(false));
    await expect(api.removePlan("a")).rejects.toMatchObject({ status: 502 });
    await expect(api.removePlan("a")).rejects.toThrow("规划删除未完成");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("每次查询使用最新岗位与用户，不复用上一人的身份", async () => {
    vi.mocked(fetch).mockImplementation(async () => ok({ items: [] }));
    await api.fetchPlanList(true);
    useIframeStore
      .getState()
      .setContext({ userId: "000456", positionId: "RB0301", bbk: "branch-b" });
    await api.fetchPlanList(true);
    const url = new URL(
      String(vi.mocked(fetch).mock.calls[1][0]),
      "https://gateway.example",
    );
    expect(Object.fromEntries(url.searchParams)).toEqual({
      sapId: "000456",
      role: "middle",
      bbkId: "branch-b",
    });
  });
});

describe("行长督办策略接口字段", () => {
  it.each([null, "plan-a"])(
    "创建/编辑 %s 提交场景督办字段并从后台返回值回显",
    async (editingId) => {
      const monitoring = {
        cycle: "within7days" as const,
        expectedCompletionRate: 80,
        schedule: {
          type: "weekly" as const,
          hour: 12,
          minute: 30,
          daysOfWeek: ["mon", "wed"],
        },
      };
      const saved = {
        ...view,
        scenes: [
          {
            ...view.scenes[0],
            schedule: 2,
            monitorTimeCron: "30 12 * * mon,wed",
            finishRate: 80,
          },
        ],
      };
      vi.mocked(fetch)
        .mockResolvedValueOnce(
          ok({ id: "plan-a", publishStatus: "publishing" }),
        )
        .mockResolvedValueOnce(ok({ items: [saved] }));
      const result = await api.publishPlan(
        account,
        { ...draft, items: [{ ...draft.items[0], monitoring }] },
        editingId,
        [],
      );
      const body = JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body));
      expect(body.scenes[0]).toMatchObject({
        schedule: 2,
        monitorTimeCron: "30 12 * * mon,wed",
        finishRate: 80,
      });
      expect(body.scenes[0]).not.toHaveProperty("monitoring");
      expect(result.plans[0].items![0].monitoring).toEqual(monitoring);
    },
  );
  it.each(["RB0101", "RB0301"])(
    "非行长 %s 即使条目带旧督办值也不发送三个字段",
    async (positionId) => {
      useIframeStore.setState({ positionId });
      vi.mocked(fetch)
        .mockResolvedValueOnce(
          ok({ id: "plan-a", publishStatus: "publishing" }),
        )
        .mockResolvedValueOnce(ok({ items: [] }));
      await api.publishPlan(
        account,
        {
          ...draft,
          items: [
            {
              ...draft.items[0],
              monitoring: {
                cycle: "today",
                expectedCompletionRate: 80,
                schedule: {
                  type: "daily",
                  hour: 9,
                  minute: 0,
                  daysOfWeek: ["mon"],
                },
              },
            },
          ],
        },
        null,
        [],
      );
      const scene = JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body))
        .scenes[0];
      for (const key of [
        "schedule",
        "monitorTimeCron",
        "finishRate",
        "monitoring",
      ])
        expect(scene).not.toHaveProperty(key);
    },
  );
  it("回显保留0%，旧规划没有督办字段时不伪造已保存策略", async () => {
    vi.mocked(fetch).mockResolvedValue(
      ok({
        items: [
          view,
          {
            ...view,
            id: "zero",
            scenes: [
              {
                ...view.scenes[0],
                schedule: 1,
                monitorTimeCron: "0 9 * * *",
                finishRate: 0,
              },
            ],
          },
        ],
      }),
    );
    const plans = await api.fetchPlanList(true);
    expect(plans[0].items![0].monitoring).toBeUndefined();
    expect(plans[1].items![0].monitoring?.expectedCompletionRate).toBe(0);
  });
});

function jsonResponse(body: BodyInit | null, init: ResponseInit = {}) {
  return new Response(body, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
}
