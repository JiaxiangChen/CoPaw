/**
 * 智能财富工作台 —— 数据访问层测试
 * 规划、场景与客户名单接口不做假数据回退：离线时读路径返回空列表、写路径上抛
 * （错误传播用例见 api.http-errors.test.ts）。
 * 本文件覆盖客户名单映射与草稿等内存行为。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import { request } from "../../api/request";
import { buildAuthHeaders } from "../../api/authHeaders";
import { useIframeStore } from "../../stores/iframeStore";

vi.mock("../../api/request", () => ({ request: vi.fn() }));

vi.mock("../../api/authHeaders", () => ({ buildAuthHeaders: vi.fn() }));

// 统一记录两种传输，验证规划请求与原生名单请求的执行顺序。
const mockRequest =
  vi.fn<(path: string, options?: RequestInit) => Promise<unknown>>();

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function mockTransports() {
  vi.mocked(request).mockImplementation(
    (path, options) => mockRequest(path, options) as never,
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, options?: RequestInit) => {
      const pathname = path.split("?")[0];
      const result = (await mockRequest(pathname, options)) as {
        items?: unknown[];
      };
      const { items, ...fields } = result;
      return new Response(
        JSON.stringify({
          code: 200,
          data: pathname.startsWith("/zt/wealth/plans")
            ? result
            : path === "/api/agent/workspace/skill-config/list"
            ? items ?? []
            : { ...fields, list: items ?? [] },
        }),
      );
    }),
  );
}

const TASK = {
  skillId: "skill-loan-1",
  sceneName: "信贷需求挖掘",
  category: "贷款",
  source: "行长关注",
};

const NAME_LIST_FIXTURE = [
  {
    custUid: "CUST001",
    custNm: "张三",
    sapId: "10086",
    filename: "http://example/cust001",
    recomReason: "命中贷款核验规则",
  },
  {
    custUid: "CUST002",
    custNm: "李四",
    sapId: "10086",
    filename: "http://example/cust002",
    recomReason: "",
  },
];

beforeEach(() => {
  api.resetMockDb();
  mockRequest.mockReset();
  useIframeStore.setState({ bbk: null, orgCode: null, positionId: null });
  mockTransports();
  vi.mocked(request).mockClear();
  vi.mocked(buildAuthHeaders).mockClear();
  mockRequest.mockImplementation(async (path: unknown) => {
    const p = String(path);
    if (p.startsWith("/api/agent/workspace/name-list")) {
      return { items: NAME_LIST_FIXTURE };
    }
    if (p.startsWith("/zt/wealth/plans")) {
      return { items: [] };
    }
    throw new Error(`未 mock 的请求：${p}`);
  });
});

describe("WealthWorkbench api", () => {
  it("名单直接 fetch 外部契约，保留上下文和统计，使用默认同源凭证策略", async () => {
    useIframeStore.setState({
      bbk: "755",
      orgCode: "755480",
      positionId: "RB0101",
    });
    const counts = {
      targetCusCount: 100,
      todayTouchCount: 0,
      sevenDayTouchCount: 25,
      unTouchedCount: 80,
    };
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          code: "200",
          data: {
            list: [...NAME_LIST_FIXTURE, { custNm: "缺少客户ID" }, null],
            ...counts,
            allFields: [{ filedName: "age", filedNameCn: "年龄" }],
          },
        }),
      ),
    );
    const result = await api.fetchNameList([" a ", "b", "a", " "], "10086", 1);
    const [path, options] = vi.mocked(fetch).mock.calls[0];
    expect(path).toBe("/api/agent/workspace/name-list");
    expect(options).not.toHaveProperty("credentials");
    expect(options).toMatchObject({
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    expect(JSON.parse(String(options?.body))).toEqual({
      skillIds: ["a", "b"],
      sapId: "10086",
      touched: 1,
      bbkId: "755",
      subBbkId: "755480",
      posId: "RB0101",
      platformSource: "WP",
      pageSource: "WP_AGENT_WORKSPACE_TASK_LIST",
    });
    expect(result).toMatchObject({
      items: NAME_LIST_FIXTURE,
      ...counts,
      allFields: [{ filedName: "age", filedNameCn: "年龄" }],
    });
    expect(request).not.toHaveBeenCalled();
    expect(buildAuthHeaders).not.toHaveBeenCalled();
  });

  it.each(["http", "business", "network", "json"])(
    "名单 %s 失败返回空列表",
    async (failure) => {
      if (failure === "network")
        vi.mocked(fetch).mockRejectedValueOnce(new Error("offline"));
      else
        vi.mocked(fetch).mockResolvedValueOnce(
          new Response(
            failure === "json"
              ? "invalid json"
              : JSON.stringify({
                  code: 500,
                  data: { list: NAME_LIST_FIXTURE },
                }),
            { status: failure === "http" ? 502 : 200 },
          ),
        );
      await expect(api.fetchNameList([TASK.skillId])).resolves.toEqual({
        items: [],
      });
      expect(request).not.toHaveBeenCalled();
    },
  );

  it("名单查询四十五秒超时后中止，失败返回空列表", async () => {
    vi.useFakeTimers();
    vi.mocked(fetch).mockImplementationOnce(
      (_path, options) =>
        new Promise((_resolve, reject) => {
          options?.signal?.addEventListener("abort", () =>
            reject(new Error("aborted")),
          );
        }),
    );
    const pending = api.fetchNameList([TASK.skillId]);
    await vi.advanceTimersByTimeAsync(44999);
    expect(vi.mocked(fetch).mock.calls[0][1]?.signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(vi.mocked(fetch).mock.calls[0][1]?.signal?.aborted).toBe(true);
    await expect(pending).resolves.toEqual({ items: [] });
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["business", "customer", "pending", "done"] as const)(
    "%s 名单保留后端统计，不根据名单长度或 touched 计算",
    async (view) => {
      const counts = {
        targetCusCount: 100,
        todayTouchCount: 0,
        sevenDayTouchCount: 25,
        unTouchedCount: 80,
      };
      mockRequest.mockResolvedValueOnce({
        items: NAME_LIST_FIXTURE,
        ...counts,
      });
      const result =
        view === "pending"
          ? await api.fetchPendingCustomers([TASK], "10086")
          : view === "done"
          ? await api.fetchDoneCustomers([TASK], "10086")
          : await api.fetchTodayCustomers([TASK], "10086", view);
      expect(result.customers).toHaveLength(2);
      expect(result.counts).toEqual(counts);
    },
  );

  it("未返回统计时保留未知状态；没有当天任务时人数为零且不发请求", async () => {
    expect((await api.fetchPendingCustomers([TASK])).counts).toEqual({
      targetCusCount: null,
      todayTouchCount: null,
      sevenDayTouchCount: null,
      unTouchedCount: null,
    });
    mockRequest.mockClear();
    expect(
      (await api.fetchTodayCustomers([], undefined, "business")).counts,
    ).toEqual({
      targetCusCount: 0,
      todayTouchCount: 0,
      sevenDayTouchCount: 0,
      unTouchedCount: 0,
    });
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("fetchNameList 传递去重后的 skillIds 数组，空数组不发起请求", async () => {
    await api.fetchNameList(["skill-a", "skill-b", "skill-a"], "10086", 0);
    expect(mockRequest).toHaveBeenCalledWith(
      "/api/agent/workspace/name-list",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json" },
      }),
    );
    expect(JSON.parse(String(mockRequest.mock.calls[0][1]?.body))).toEqual({
      skillIds: ["skill-a", "skill-b"],
      bbkId: "",
      platformSource: "WP",
      pageSource: "WP_AGENT_WORKSPACE_TASK_LIST",
      touched: 0,
      sapId: "10086",
    });
    mockRequest.mockClear();
    await expect(api.fetchNameList([], "10086", 2)).resolves.toEqual({
      items: [],
    });
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("动态字段按技能配置排序，只展示客户自己的值，保留组合分组及触达状态", async () => {
    const response = {
      items: [
        {
          ...NAME_LIST_FIXTURE[0],
          touched: 1,
          fieldList: [
            { filedName: "age", filedNameCn: "旧年龄名称", filedValue: 0 },
            { filedName: "risk", filedNameCn: "风险等级", filedValue: null },
            {
              filedName: "other",
              filedNameCn: "其他技能字段",
              filedValue: "不展示",
            },
          ],
        },
      ],
      skillFieldList: [
        {
          skillId: TASK.skillId,
          groupField: ["age", "missing"],
          fields: [
            { filedName: "risk", filedNameCn: "风险等级" },
            { filedName: "age", filedNameCn: "年龄" },
            { filedName: "missing", filedNameCn: "缺失字段" },
          ],
        },
      ],
      allFields: [
        { filedName: "age", filedNameCn: "客户年龄" },
        { filedName: "risk", filedNameCn: "风险等级" },
        { filedName: "missing", filedNameCn: "缺失字段" },
        { filedName: "other", filedNameCn: "其他技能字段" },
      ],
    };
    mockRequest.mockResolvedValue(response);
    const {
      customers: [business],
    } = await api.fetchTodayCustomers([TASK], "10086", "business");
    expect(business.done).toBe(true);
    expect(business.dynamicFields).toEqual([
      { name: "risk", label: "风险等级", value: "未提供" },
      { name: "age", label: "客户年龄", value: "0" },
    ]);
    expect(business.groupFields).toEqual([
      { name: "age", label: "客户年龄", value: "0" },
      { name: "missing", label: "缺失字段", value: "未提供" },
    ]);
    const {
      customers: [customer],
    } = await api.fetchTodayCustomers([TASK], "10086", "customer");
    expect(customer.dynamicFields?.map((f) => f.name)).toEqual([
      "age",
      "risk",
      "other",
    ]);
    expect(customer.groupFields).toEqual([]);
    expect(customer.done).toBe(true);
    const {
      customers: [done],
    } = await api.fetchDoneCustomers([TASK], "10086");
    expect(done.dynamicFields).toEqual(customer.dynamicFields);
    mockRequest.mockResolvedValueOnce({
      ...response,
      items: response.items.map((item) => ({ ...item, touched: 0 })),
    });
    const {
      customers: [pending],
    } = await api.fetchPendingCustomers([TASK], "10086");
    expect(pending.dynamicFields).toEqual(customer.dynamicFields);
  });

  it("fetchBootstrap 返回空草稿；客户名单与触达历史均不在 bootstrap 内", async () => {
    const data = await api.fetchBootstrap("rm");
    expect(data.plans).toEqual([]);
    expect(data.draft).toEqual({ name: "", items: [] });
    expect(data.savedAt).toBe("");
    expect("customers" in data).toBe(false);
    expect("history" in data).toBe(false);
  });

  it("大量技能通过一次 POST 请求体传递，URL 保持固定", async () => {
    const ids = Array.from({ length: 2000 }, (_, index) => `skill-${index}`);
    await api.fetchNameList(ids, "10086", 2);
    expect(mockRequest).toHaveBeenCalledTimes(1);
    const [path, options] = mockRequest.mock.calls[0];
    expect(path).toBe("/api/agent/workspace/name-list");
    expect(options?.method).toBe("POST");
    expect(JSON.parse(String(options?.body)).skillIds).toEqual(ids);
  });

  it("fetchNameList 透传 skillId 与 sapId；接口失败返回空列表", async () => {
    const list = await api.fetchNameList(["skill-loan-1"], "10086");
    expect(list.items).toHaveLength(2);
    expect(JSON.parse(String(mockRequest.mock.calls[0]?.[1]?.body))).toEqual({
      skillIds: ["skill-loan-1"],
      sapId: "10086",
      bbkId: "",
      platformSource: "WP",
      pageSource: "WP_AGENT_WORKSPACE_TASK_LIST",
    });

    await api.fetchNameList(["skill-loan-1"]);
    expect(
      JSON.parse(String(mockRequest.mock.calls[1]?.[1]?.body)),
    ).not.toHaveProperty("sapId");

    mockRequest.mockRejectedValueOnce(new Error("boom"));
    await expect(api.fetchNameList(["skill-loan-1"])).resolves.toEqual({
      items: [],
    });
  });

  it("场景直接 POST 同源接口，传分行与分类并映射外部 data 数组", async () => {
    useIframeStore.setState({ bbk: "755" });
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          code: "200",
          data: [
            {
              skillId: "scene-1",
              itemId: "item-1",
              senceName: "理财到期",
              category: "finance",
              senceDesc: "到期提醒",
              cronExample: "每日提醒",
              mcpRelationList: ["mcp-1"],
              skillBbkLabel: "总部预置",
              bbkId: "100",
            },
          ],
        }),
      ),
    );
    await expect(api.fetchScenesByCategory("finance")).resolves.toEqual([
      {
        id: "scene-1",
        itemId: "item-1",
        name: "理财到期",
        category: "理财",
        categoryCode: "finance",
        icon: "chart",
        desc: "到期提醒",
        source: "总部预置",
        cronExample: "每日提醒",
        mcpRelations: ["mcp-1"],
      },
    ]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/agent/workspace/skill-config/list",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bbkId: "755", category: "finance" }),
        signal: expect.any(AbortSignal),
      },
    );
    expect(request).not.toHaveBeenCalled();
    expect(buildAuthHeaders).not.toHaveBeenCalled();
  });

  it.each([
    ["HTTP", () => new Response("error", { status: 500 })],
    ["业务", () => new Response(JSON.stringify({ code: 500, data: [] }))],
    ["JSON", () => new Response("invalid json")],
  ])("场景 %s 失败时列表为空，能力数保持未知", async (_label, response) => {
    vi.mocked(fetch).mockImplementation(async () => response());
    await expect(api.fetchScenesByCategory("finance")).resolves.toEqual([]);
    await expect(api.fetchAvailableSceneCount()).resolves.toBeNull();
  });

  it("场景超时取消请求并返回空列表", async () => {
    vi.useFakeTimers();
    vi.mocked(fetch).mockImplementation(
      (_path, options) =>
        new Promise((_resolve, reject) => {
          options?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    const result = api.fetchScenesByCategory("");
    await vi.advanceTimersByTimeAsync(60000);
    await expect(result).resolves.toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("fetchAvailableSceneCount 返回全部大类场景数；失败返回 null", async () => {
    mockRequest.mockResolvedValueOnce({
      items: [{ skillId: "a" }, { skillId: "b" }, { skillId: "c" }],
    });
    await expect(api.fetchAvailableSceneCount()).resolves.toBe(3);
    expect(String(mockRequest.mock.calls[0]?.[0])).toBe(
      "/api/agent/workspace/skill-config/list",
    );

    expect(JSON.parse(String(mockRequest.mock.calls[0]?.[1]?.body))).toEqual({
      bbkId: "",
      category: "",
    });
    expect(request).not.toHaveBeenCalled();
    mockRequest.mockRejectedValueOnce(new Error("boom"));
    await expect(api.fetchAvailableSceneCount()).resolves.toBeNull();
  });

  it("规划统计直连同源路径，按 planId 映射乱序返回且不走后端", async () => {
    document.cookie = "token=skill-stats-token; path=/";
    vi.mocked(buildAuthHeaders).mockReturnValue({
      "X-Bbk-Id": "100",
      "X-User-Id": "rm-1",
      "X-Position-Id": "RB0101",
      "x-header-cookie": "session=value",
    });
    const directFetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          code: "200",
          data: {
            items: [
              { planId: "p2", targetCustomerCount: 89, targetTaskCount: 17 },
              { planId: "p1", targetCustomerCount: 125, targetTaskCount: 30 },
            ],
          },
        }),
        { status: 200 },
      ),
    );
    try {
      const controller = new AbortController();
      expect(
        await api.fetchSkillStats(["p1", "p2", "p1"], controller.signal),
      ).toEqual({
        p1: { targetCustomerCount: 125, generatedTaskCount: 30 },
        p2: { targetCustomerCount: 89, generatedTaskCount: 17 },
      });
      const [path, options] = directFetch.mock.calls[0];
      expect(path).toBe("/api/agent/workspace/skill-stats");
      expect(options).toMatchObject({
        method: "POST",
        credentials: "include",
        signal: controller.signal,
      });
      expect(options?.headers).toEqual({
        "Content-Type": "application/json",
        Authorization: "skill-stats-token",
        "x-header-cookie": "session=value",
        Cookie: "session=value",
      });
      expect(JSON.parse(String(options?.body))).toEqual({
        planIds: ["p1", "p2"],
      });
      expect(mockRequest).not.toHaveBeenCalled();

      directFetch.mockClear();
      await expect(api.fetchSkillStats([])).resolves.toEqual({});
      expect(directFetch).not.toHaveBeenCalled();
    } finally {
      document.cookie = "token=; Max-Age=0; path=/";
      directFetch.mockRestore();
    }
  });

  it.each(["RB0101", "RB1101", "RB0301"])(
    "%s 统计请求体只传 planIds，保留零值",
    async (positionId) => {
      vi.mocked(buildAuthHeaders).mockReturnValue({
        "X-Bbk-Id": "100",
        "X-User-Id": "manager",
        "X-Position-Id": positionId,
      });
      const directFetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
        new Response(
          JSON.stringify({
            code: 200,
            data: {
              items: [
                { planId: "p1", targetCustomerCount: 0, targetTaskCount: 0 },
              ],
            },
          }),
        ),
      );
      try {
        await expect(api.fetchSkillStats(["p1"])).resolves.toEqual({
          p1: { targetCustomerCount: 0, generatedTaskCount: 0 },
        });
        expect(JSON.parse(String(directFetch.mock.calls[0][1]?.body))).toEqual({
          planIds: ["p1"],
        });
      } finally {
        directFetch.mockRestore();
      }
    },
  );

  it.each(["http", "business", "network", "aborted"])(
    "统计 %s 失败返回 null",
    async (failure) => {
      vi.mocked(buildAuthHeaders).mockReturnValue({
        "X-Position-Id": "RB0101",
      });
      const directFetch = vi.spyOn(globalThis, "fetch");
      if (failure === "network" || failure === "aborted")
        directFetch.mockRejectedValueOnce(new Error(failure));
      else
        directFetch.mockResolvedValueOnce(
          new Response(JSON.stringify({ code: 500 }), {
            status: failure === "http" ? 502 : 200,
          }),
        );
      try {
        await expect(api.fetchSkillStats(["p1"])).resolves.toBeNull();
      } finally {
        directFetch.mockRestore();
      }
    },
  );

  it.each(["business", "customer"] as const)(
    "今日任务 %s 保留原始 touched，包括未知或缺失状态",
    async (view) => {
      const statuses = [0, 1, 3, 2, -1, null, undefined];
      mockRequest.mockResolvedValueOnce({
        items: statuses.map((touched, index) => ({
          custUid: `cust-${index}`,
          custNm: `客户${index}`,
          touched,
        })),
      });
      const { customers: customers } = await api.fetchTodayCustomers(
        [TASK],
        "10086",
        view,
      );
      expect(customers.map((customer) => customer.touched)).toEqual(statuses);
      expect(customers.map((customer) => customer.done)).toEqual([
        false,
        true,
        false,
        false,
        false,
        false,
        false,
      ]);
    },
  );

  it("fetchTodayCustomers 按任务上下文映射名单并去重", async () => {
    const { customers: customers } = await api.fetchTodayCustomers(
      [
        TASK,
        { ...TASK }, // 同一技能重复任务只查一次、只出一条
      ],
      "10086",
      "business",
    );
    expect(customers).toHaveLength(2);
    expect(customers[0]).toMatchObject({
      id: "skill-loan-1|CUST001",
      custUid: "CUST001",
      skillId: "skill-loan-1",
      name: "张三",
      task: "信贷需求挖掘",
      category: "贷款",
      reason: "命中贷款核验规则",
      link: "http://example/cust001",
      done: false,
    });
    const nameListCalls = mockRequest.mock.calls.filter(([p]) =>
      String(p).startsWith("/api/agent/workspace/name-list"),
    );
    expect(nameListCalls).toHaveLength(1);
    expect(JSON.parse(String(nameListCalls[0]?.[1]?.body)).sapId).toBe("10086");
  });

  it("fetchTodayCustomers 客户视角：直接映射外部聚合名单并标注命中场景", async () => {
    mockRequest.mockImplementation(async (path: unknown) => {
      const p = String(path);
      if (p.startsWith("/api/agent/workspace/name-list")) {
        return {
          items: [
            {
              ...NAME_LIST_FIXTURE[0],
              skillList: [
                { skillId: "skill-loan-1", skillName: "信贷需求挖掘" },
                { skillId: "skill-unknown", skillName: "接口额外技能" },
                { skillId: "skill-loan-1", skillName: "信贷需求挖掘" },
              ],
              strongContactTime: "2026-09-16 10:30:00",
              touchMethod: "电话",
            },
            {
              ...NAME_LIST_FIXTURE[1],
              skillList: [{ skillId: "skill-unknown", skillName: "未知技能" }],
            },
          ],
        };
      }
      throw new Error(`未 mock 的请求：${p}`);
    });

    const { customers: customers } = await api.fetchTodayCustomers(
      [TASK],
      "10086",
      "customer",
    );

    const nameListCalls = mockRequest.mock.calls.filter(([p]) =>
      String(p).startsWith("/api/agent/workspace/name-list"),
    );
    expect(nameListCalls).toHaveLength(1);
    expect(JSON.parse(String(nameListCalls[0]?.[1]?.body)).skillIds).toEqual([
      TASK.skillId,
    ]);
    expect(JSON.parse(String(nameListCalls[0]?.[1]?.body)).sapId).toBe("10086");
    // 以 custUid 为页面 id；重点标签按规划创建角色映射，树外技能不产生标签
    expect(customers[0]).toMatchObject({
      id: "CUST001",
      name: "张三",
      label: "行长指派",
      task: "信贷需求挖掘",
      skillNames: ["信贷需求挖掘", "接口额外技能"],
      category: "贷款",
      reason: "命中贷款核验规则",
      opportunities: ["命中贷款核验规则"],
      channel: "电话",
      time: "2026-09-16 10:30:00",
    });
    expect(customers[1]).toMatchObject({
      id: "CUST002",
      label: "",
      task: "",
      skillNames: ["未知技能"],
      reason: "",
      opportunities: [],
    });
  });

  it("客户命中多个规划来源时展示去重后的角色重点标签", async () => {
    mockRequest.mockResolvedValueOnce({
      items: [
        {
          ...NAME_LIST_FIXTURE[0],
          skillList: [
            { skillId: "skill-president", skillName: "行长任务" },
            { skillId: "skill-middle", skillName: "分行任务" },
            { skillId: "skill-rm", skillName: "客户经理任务" },
            { skillId: "skill-president-2", skillName: "另一行长任务" },
          ],
        },
      ],
    });

    const { customers: customers } = await api.fetchTodayCustomers(
      [
        { ...TASK, skillId: "skill-president", source: "行长关注" },
        { ...TASK, skillId: "skill-middle", source: "分行关注" },
        { ...TASK, skillId: "skill-rm", source: "我的关注" },
        { ...TASK, skillId: "skill-president-2", source: "行长关注" },
      ],
      "10086",
      "customer",
    );

    expect(customers[0]?.label).toBe("行长指派、分行重点、我的关注");
  });

  it("fetchPendingCustomers / fetchDoneCustomers 分别传递触达状态参数", async () => {
    await api.fetchPendingCustomers([TASK], "10086");
    await api.fetchDoneCustomers([TASK], "10086");
    const calls = mockRequest.mock.calls.map(([, options]) =>
      JSON.parse(String(options?.body)),
    );
    expect(calls[0].touched).toBe(0);
    expect(calls[1].touched).toBe(1);
    // 两个口径都带当天技能数组和 sapId
    for (const c of calls) {
      expect(c.skillIds).toEqual([TASK.skillId]);
      expect(c.sapId).toBe("10086");
    }
  });

  it("fetchPendingCustomers 使用接口返回的待触达名单，不依赖明细触达字段筛选", async () => {
    mockRequest.mockResolvedValueOnce({
      items: NAME_LIST_FIXTURE,
    });

    const { customers: pending } = await api.fetchPendingCustomers(
      [TASK],
      "10086",
    );

    expect(pending).toHaveLength(2);
    expect(pending.every((customer) => !customer.done)).toBe(true);
  });

  it("fetchDoneCustomers 使用接口返回的已完成名单，不依赖明细触达字段筛选", async () => {
    mockRequest.mockResolvedValueOnce({
      items: NAME_LIST_FIXTURE,
    });

    const { customers: done } = await api.fetchDoneCustomers([TASK], "10086");

    expect(done).toHaveLength(2);
    expect(done.every((customer) => customer.done)).toBe(true);
  });

  it("fetchDoneCustomers 使用 skillName 作为经营任务", async () => {
    mockRequest.mockResolvedValueOnce({
      items: [
        {
          ...NAME_LIST_FIXTURE[0],
          touched: 1,
          skillList: [
            { skillId: "skill-loan-1", skillName: "外部经营任务名称" },
          ],
        },
      ],
    });
    const { customers: done } = await api.fetchDoneCustomers([TASK], "10086");
    expect(done).toHaveLength(1);
    expect(done.every((c) => c.done)).toBe(true);
    expect(done[0]).toMatchObject({
      task: "外部经营任务名称",
      channel: "",
      time: "",
      note: "",
    });
  });

  it("resetMockDb 后草稿恢复初始（模拟刷新）", async () => {
    await api.saveDraft("rm", { name: "改过的草稿", items: [] });
    api.resetMockDb();
    const data = await api.fetchBootstrap("rm");
    expect(data.draft).toEqual({ name: "", items: [] });
  });

  it("saveDraft 按账户隔离草稿并记录保存时间", async () => {
    const draft = { name: "行长的草稿", items: [] };
    const { savedAt } = await api.saveDraft("president", draft);
    expect(savedAt).toMatch(/^\d{2}:\d{2}$/);
    const data = await api.fetchBootstrap("president");
    expect(data.draft.name).toBe("行长的草稿");
    expect(data.savedAt).toBe(savedAt);
    // 其他账户不受影响
    const rmData = await api.fetchBootstrap("rm");
    expect(rmData.draft).toEqual({ name: "", items: [] });
  });

  it("fetchPlanList 规划接口不可达时返回空列表（不回退假数据）", async () => {
    mockRequest.mockRejectedValue(new Error("boom"));
    expect(await api.fetchPlanList()).toEqual([]);
  });

  it("fetchScenesByCategory 场景接口不可达时返回空列表（不做假数据兜底）", async () => {
    mockRequest.mockRejectedValue(new Error("boom"));
    expect(await api.fetchScenesByCategory("insurance")).toEqual([]);
    expect(await api.fetchScenesByCategory("")).toEqual([]);
  });
});
