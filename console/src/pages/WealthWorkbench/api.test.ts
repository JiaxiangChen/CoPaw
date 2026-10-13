/**
 * 智能财富工作台 —— 数据访问层测试
 * 规划、场景与客户名单接口不做假数据回退：离线时读路径返回空列表、写路径上抛
 * （错误传播用例见 api.http-errors.test.ts）。
 * 本文件覆盖客户名单映射与草稿等内存行为。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import { request } from "../../api/request";

vi.mock("../../api/request", () => ({ request: vi.fn() }));

const mockRequest = vi.mocked(request);

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
  mockRequest.mockImplementation(async (path: unknown) => {
    const p = String(path);
    if (p.startsWith("/wealth/name-list")) {
      return { items: NAME_LIST_FIXTURE };
    }
    if (p.startsWith("/wealth/plans")) {
      return { items: [] };
    }
    throw new Error(`未 mock 的请求：${p}`);
  });
});

describe("WealthWorkbench api", () => {
  it("动态字段按技能配置排序，只展示客户自己的值，保留组合分组及触达状态", async () => {
    mockRequest.mockResolvedValue({
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
    });
    const [business] = await api.fetchTodayCustomers(
      [TASK],
      "10086",
      "business",
    );
    expect(business.done).toBe(true);
    expect(business.dynamicFields).toEqual([
      { name: "risk", label: "风险等级", value: "未提供" },
      { name: "age", label: "客户年龄", value: "0" },
    ]);
    expect(business.groupFields).toEqual([
      { name: "age", label: "客户年龄", value: "0" },
      { name: "missing", label: "缺失字段", value: "未提供" },
    ]);
    const [customer] = await api.fetchTodayCustomers(
      [TASK],
      "10086",
      "customer",
    );
    expect(customer.dynamicFields?.map((f) => f.name)).toEqual([
      "age",
      "risk",
      "other",
    ]);
    expect(customer.groupFields).toEqual([]);
    expect(customer.done).toBe(true);
    const [done] = await api.fetchDoneCustomers([TASK], "10086");
    expect(done.dynamicFields).toEqual(customer.dynamicFields);
    const [pending] = await api.fetchPendingCustomers([TASK], "10086");
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

  it("fetchNameList 透传 skillId 与 sapId；接口失败返回空列表", async () => {
    const list = await api.fetchNameList("skill-loan-1", "10086");
    expect(list.items).toHaveLength(2);
    expect(mockRequest.mock.calls[0]?.[0]).toContain("skill_id=skill-loan-1");
    expect(mockRequest.mock.calls[0]?.[0]).toContain("sap_id=10086");

    await api.fetchNameList("skill-loan-1");
    expect(mockRequest.mock.calls[1]?.[0]).not.toContain("sap_id=");

    mockRequest.mockRejectedValueOnce(new Error("boom"));
    await expect(api.fetchNameList("skill-loan-1")).resolves.toEqual({
      items: [],
    });
  });

  it("fetchAvailableSceneCount 返回全部大类场景数；失败返回 null", async () => {
    mockRequest.mockResolvedValueOnce({
      items: [{ skillId: "a" }, { skillId: "b" }, { skillId: "c" }],
    });
    await expect(api.fetchAvailableSceneCount()).resolves.toBe(3);
    expect(String(mockRequest.mock.calls[0]?.[0])).toBe(
      "/wealth/scene-skills?category=",
    );

    mockRequest.mockRejectedValueOnce(new Error("boom"));
    await expect(api.fetchAvailableSceneCount()).resolves.toBeNull();
  });

  it("fetchSkillStats 按场景统计键映射结果；失败返回 null", async () => {
    mockRequest.mockResolvedValueOnce({
      items: [
        { skillId: "s1", targetCustomerCount: 125, generatedTaskCount: 30 },
        { skillId: "s2", targetCustomerCount: 89, generatedTaskCount: 17 },
      ],
    });
    const queries = [
      { skillId: "s1", startDate: "2026-09-01", endDate: "2026-09-30" },
      { skillId: "s2", startDate: "2026-09-05", endDate: "2026-09-15" },
    ];
    const stats = await api.fetchSkillStats(queries);
    expect(stats).toEqual({
      "s1|2026-09-01|2026-09-30": {
        targetCustomerCount: 125,
        generatedTaskCount: 30,
      },
      "s2|2026-09-05|2026-09-15": {
        targetCustomerCount: 89,
        generatedTaskCount: 17,
      },
    });
    const [path, options] = mockRequest.mock.calls[0] ?? [];
    expect(String(path)).toBe("/wealth/skill-stats");
    expect(JSON.parse(String((options as RequestInit).body))).toEqual({
      skills: queries,
    });

    mockRequest.mockRejectedValueOnce(new Error("boom"));
    await expect(api.fetchSkillStats(queries)).resolves.toBeNull();

    // 空查询不发请求
    await expect(api.fetchSkillStats([])).resolves.toEqual({});
  });

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
      const customers = await api.fetchTodayCustomers([TASK], "10086", view);
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
    const customers = await api.fetchTodayCustomers(
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
      String(p).startsWith("/wealth/name-list"),
    );
    expect(nameListCalls).toHaveLength(1);
    expect(String(nameListCalls[0]?.[0])).toContain("sap_id=10086");
  });

  it("fetchTodayCustomers 客户视角：直接映射外部聚合名单并标注命中场景", async () => {
    mockRequest.mockImplementation(async (path: unknown) => {
      const p = String(path);
      if (p.startsWith("/wealth/name-list")) {
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

    const customers = await api.fetchTodayCustomers(
      [TASK],
      "10086",
      "customer",
    );

    const nameListCalls = mockRequest.mock.calls.filter(([p]) =>
      String(p).startsWith("/wealth/name-list"),
    );
    expect(nameListCalls).toHaveLength(1);
    expect(String(nameListCalls[0]?.[0])).not.toContain("skill_id=");
    expect(String(nameListCalls[0]?.[0])).toContain("sap_id=10086");
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

    const customers = await api.fetchTodayCustomers(
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

  it("fetchPendingCustomers / fetchDoneCustomers 以 touched 区分名单口径", async () => {
    await api.fetchPendingCustomers([TASK], "10086");
    await api.fetchDoneCustomers([TASK], "10086");
    const calls = mockRequest.mock.calls.map(([p]) => String(p));
    expect(calls[0]).toContain("touched=0");
    expect(calls[1]).toContain("touched=2");
    // 两个口径都不带 skillId（客户视角粒度），都带 sapId
    for (const c of calls) {
      expect(c).not.toContain("skill_id=");
      expect(c).toContain("sap_id=10086");
    }
  });

  it("fetchDoneCustomers 查询全部名单后仅保留 touched 为 1 的客户", async () => {
    mockRequest.mockResolvedValueOnce({
      items: [0, 1, 2, undefined, null, "1"].map((touched, index) => ({
        ...NAME_LIST_FIXTURE[0],
        custUid: `CUST${index}`,
        touched,
      })),
    });

    const done = await api.fetchDoneCustomers([TASK], "10086");

    expect(done).toHaveLength(1);
    expect(done[0]).toMatchObject({ custUid: "CUST1", touched: 1, done: true });
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
    const done = await api.fetchDoneCustomers([TASK], "10086");
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
