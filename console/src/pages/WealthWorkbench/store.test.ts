/**
 * 智能财富工作台 —— store 行为测试
 * request 层用内存夹具 mock（规划接口已不做假数据回退）；
 * 场景接口按 category 返回 SceneSkillItem 夹具，账户/草稿走真实实现。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetMockDb } from "./api";
import { request } from "../../api/request";
import { useIframeStore } from "../../stores/iframeStore";
import {
  useWealthStore,
  selectCurrentAccount,
  validateDraft,
  planScheduledOn,
} from "./store";
import { cycleRange, DEFAULT_SCHEDULE, todayKey } from "./utils";
import type { Customer, Plan, PlanItem } from "./types";

vi.mock("../../api/request", () => ({ request: vi.fn() }));

// 统一记录两种传输，验证规划请求与原生名单请求的执行顺序。
const mockRequest =
  vi.fn<(path: string, options?: RequestInit) => Promise<unknown>>();

afterEach(() => vi.unstubAllGlobals());

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

const INSURANCE = "skill-wealth-insurance-1";
const INSURANCE_2 = "skill-wealth-insurance-2";
const FINANCE = "skill-wealth-finance-3";
const LOAN = "skill-wealth-loan-4";

/** 客户名单夹具：经营视角仅 LOAN 技能返回两个客户；客户视角使用外部聚合结果的 skillList */
const NAME_LIST_FIXTURE = [
  {
    custUid: "CUST001",
    custNm: "张三",
    sapId: "10086",
    filename: "http://example/cust001",
    recomReason: "命中贷款核验规则",
    skillList: [{ skillId: LOAN, skillName: "信贷需求挖掘" }],
  },
  {
    custUid: "CUST002",
    custNm: "李四",
    sapId: "10086",
    filename: "http://example/cust002",
    recomReason: "",
    skillList: [{ skillId: LOAN, skillName: "信贷需求挖掘" }],
  },
];

/** 后端 /zt/wealth/plans 的返回形状（PlanView 夹具） */
interface FixturePlanView {
  id: string;
  name: string;
  description?: string | null;
  sourceLabel?: string | null;
  periodStart?: string | null;
  periodEnd?: string | null;
  publishStatus: string;
  boardStatus: string;
  editable: boolean;
  scenes: {
    sceneId: string;
    sceneName: string;
    category: string;
    categoryLabel: string;
    itemId?: string | null;
    direction?: string | null;
    cycle?: string | null;
    startDate?: string | null;
    endDate?: string | null;
    cronExpr: string;
    mcpRelations: string[];
  }[];
  targets: { sapId: string; name?: string | null }[];
}

let planViews: FixturePlanView[];

function makeView(patch: Partial<FixturePlanView> = {}): FixturePlanView {
  return {
    id: "plan-x",
    name: "规划",
    sourceLabel: "分行关注",
    periodStart: "2026-09-01",
    periodEnd: "2026-09-30",
    publishStatus: "published",
    boardStatus: "已自动下发",
    editable: true,
    scenes: [],
    targets: [],
    ...patch,
  };
}

function fixturePlanViews(): FixturePlanView[] {
  return [
    makeView({ id: "plan-demo-1", name: "保险重点经营" }),
    makeView({
      id: "plan-demo-2",
      name: "产品到期承接",
      sourceLabel: "行长关注",
      scenes: [
        {
          sceneId: FINANCE,
          sceneName: "产品到期承接",
          category: "finance",
          categoryLabel: "理财",
          itemId: "item-wealth-finance-3",
          direction: "优先承接近期到期资金",
          cycle: "本月",
          startDate: "2026-09-01",
          endDate: "2026-09-30",
          cronExpr: "0 9 * * *",
          mcpRelations: ["mcp-wealth"],
        },
      ],
    }),
    makeView({ id: "plan-demo-3", name: "代发客户经营" }),
    makeView({ id: "plan-demo-4", name: "高价值动账" }),
    makeView({ id: "plan-demo-5", name: "理财重点客户经营" }),
    makeView({
      id: "plan-demo-6",
      name: "基金定投提升",
      publishStatus: "publishing",
      boardStatus: "发布中",
    }),
  ];
}

/** 场景接口夹具：SceneSkillItem 形状，按 category 过滤，空串返回全部 */
const SCENE_FIXTURES = [
  {
    skillId: INSURANCE,
    itemId: "item-wealth-insurance-1",
    senceName: "保障潜客经营",
    category: "insurance",
    senceDesc: "挖掘高潜保险客户",
    cronExample: "每日生成高潜保险客户名单",
    mcpRelationList: ["mcp-customer", "mcp-insurance"],
    skillBbkLabel: "总部预置",
  },
  {
    skillId: INSURANCE_2,
    itemId: "item-wealth-insurance-2",
    senceName: "保障缺口经营",
    category: "insurance",
    senceDesc: "识别客户保障缺口",
    mcpRelationList: ["mcp-customer", "mcp-insurance"],
    skillBbkLabel: "总部预置",
  },
  {
    skillId: FINANCE,
    itemId: "item-wealth-finance-3",
    senceName: "产品到期承接",
    category: "finance",
    senceDesc: "优先承接近期到期资金",
    mcpRelationList: ["mcp-wealth"],
    skillBbkLabel: "总部预置",
  },
  {
    skillId: LOAN,
    itemId: "item-wealth-loan-4",
    senceName: "信贷需求挖掘",
    category: "loan",
    senceDesc: "识别潜在信贷客户",
    mcpRelationList: ["mcp-loan"],
    skillBbkLabel: "总部预置",
  },
];

/** request 夹具：规划 CRUD 走内存表；场景接口按 category 参数返回夹具 */
async function requestHandler(
  path: string,
  options: RequestInit = {},
): Promise<unknown> {
  const method = options.method ?? "GET";
  if (path.startsWith("/api/agent/workspace/skill-config/list")) {
    const category = JSON.parse(String(options.body)).category ?? "";
    return {
      items: category
        ? SCENE_FIXTURES.filter((s) => s.category === category)
        : SCENE_FIXTURES,
    };
  }
  if (path.startsWith("/api/agent/workspace/name-list")) {
    const skillIds = JSON.parse(String(options.body)).skillIds as string[];
    return { items: skillIds.includes(LOAN) ? NAME_LIST_FIXTURE : [] };
  }
  if (path === "/zt/wealth/plans" && method === "GET") {
    return { items: planViews };
  }
  if (path === "/zt/wealth/plans" && method === "POST") {
    const body = JSON.parse(String(options.body));
    const view = makeView({
      id: `plan-new-${planViews.length}`,
      name: body.name,
      sourceLabel: body.sourceLabel,
      publishStatus: "publishing",
      boardStatus: "发布中",
      targets: body.targets,
    });
    planViews = [view, ...planViews];
    return { id: view.id, publishStatus: view.publishStatus };
  }
  const match = /^\/zt\/wealth\/plans\/([^/]+)$/.exec(path);
  if (match && method === "DELETE") {
    const id = decodeURIComponent(match[1]);
    planViews = planViews.filter((p) => p.id !== id);
    return true;
  }
  throw new Error(`未 mock 的请求：${method} ${path}`);
}

/** 构造通过校验的草稿条目 */
function makeItem(patch: Partial<PlanItem> = {}): PlanItem {
  return {
    id: INSURANCE,
    sceneName: "保障潜客经营",
    categoryLabel: "保险",
    categoryCode: "insurance",
    cronExample: "每日生成高潜保险客户名单",
    mcpRelations: [],
    direction: "d",
    cycle: "本月",
    start: "2026-09-01",
    end: "2026-09-30",
    schedule: { ...DEFAULT_SCHEDULE },
    ...patch,
  };
}

/** 构造一个「今天有排程」的已发布规划（日期按运行当天动态计算，保证测试与日期无关） */
function makeTodayPlan(): Plan {
  const today = todayKey();
  return {
    id: "plan-today",
    name: "今日规划",
    source: "我的关注",
    desc: "",
    customers: 0,
    tasks: 0,
    rate: 0,
    status: "已自动下发",
    publishStatus: "published",
    period: "自定义",
    start: today,
    end: today,
    items: [
      makeItem({
        id: LOAN,
        sceneName: "信贷需求挖掘",
        categoryLabel: "贷款",
        categoryCode: "loan",
        start: today,
        end: today,
      }),
    ],
  };
}

function makeTodayPlanView(): FixturePlanView {
  const today = todayKey();
  return makeView({
    periodStart: today,
    periodEnd: today,
    scenes: [
      {
        sceneId: LOAN,
        sceneName: "信贷需求挖掘",
        category: "loan",
        categoryLabel: "贷款",
        startDate: today,
        endDate: today,
        cronExpr: "0 9 * * *",
        mcpRelations: [],
      },
    ],
  });
}

async function initStore() {
  resetMockDb();
  mockRequest.mockReset();
  mockTransports();
  planViews = fixturePlanViews();
  mockRequest.mockImplementation(requestHandler as never);
  // 显式声明测试身份为客户经理（RB0101），不依赖 FALLBACK_ROLE 兜底
  useIframeStore.setState({ positionId: "RB0101", userId: null });
  useWealthStore.setState({
    initialized: false,
    accountId: "rm",
    scenesByCategory: {},
    scenesLoading: false,
    plans: [],
    customers: [],
    todaySkillId: "",
    customersLoading: false,
    pendingCustomers: [],
    pendingLoading: false,
    doneCustomers: [],
    doneLoading: false,
    draft: { name: "", items: [] },
    savedAt: "",
    editingId: null,
    targets: [],
    targetsLoaded: false,
    targetSapIds: [],
    dialog: null,
    acting: false,
    toastText: "",
    toastSeq: 0,
  });
  await useWealthStore.getState().init();
}

/** 直接塞入一份可发布的草稿（替代已移除的预填 mock 草稿） */
function seedDraft() {
  useWealthStore.setState({
    draft: {
      name: "九月规划",
      items: [
        makeItem(),
        makeItem({
          id: FINANCE,
          sceneName: "产品到期承接",
          categoryLabel: "理财",
          categoryCode: "finance",
        }),
      ],
    },
  });
}

describe("WealthWorkbench store", () => {
  it("三个模块的名单和统计一起保存，经营视角统计不被聚合查询覆盖", async () => {
    planViews = [makeTodayPlanView()];
    await useWealthStore.getState().refreshPlans();
    const counts = {
      targetCusCount: 100,
      todayTouchCount: 4,
      sevenDayTouchCount: 20,
      unTouchedCount: 90,
    };
    mockRequest.mockResolvedValueOnce({ items: NAME_LIST_FIXTURE, ...counts });
    mockRequest.mockResolvedValueOnce({
      items: NAME_LIST_FIXTURE,
      ...counts,
      targetCusCount: 200,
    });
    await useWealthStore.getState().loadTodayCustomers("business", LOAN);
    expect(useWealthStore.getState().todayCounts).toEqual(counts);
    const pendingCounts = { ...counts, unTouchedCount: 80 };
    mockRequest.mockResolvedValueOnce({
      items: NAME_LIST_FIXTURE,
      ...pendingCounts,
    });
    await useWealthStore.getState().loadPendingCustomers();
    const doneCounts = { ...counts, sevenDayTouchCount: 30 };
    mockRequest.mockResolvedValueOnce({
      items: NAME_LIST_FIXTURE,
      ...doneCounts,
    });
    await useWealthStore.getState().loadDoneCustomers();
    expect(useWealthStore.getState().pendingCounts).toEqual(pendingCounts);
    expect(useWealthStore.getState().doneCounts).toEqual(doneCounts);
    expect(useWealthStore.getState().todayCounts).toEqual(counts);
  });

  it("当天没有工作任务时，各模块及角标清空且不查询范围外客户", async () => {
    useWealthStore.setState({ plans: [], todayCustomerCount: 9 });
    mockRequest.mockClear();
    const state = useWealthStore.getState();
    await state.loadTodayCustomers("business");
    await state.loadPendingCustomers();
    await state.loadDoneCustomers();
    await state.loadTodayCustomerCount();
    expect(mockRequest).not.toHaveBeenCalled();
    expect(useWealthStore.getState()).toMatchObject({
      customers: [],
      pendingCustomers: [],
      doneCustomers: [],
      todayCustomerCount: 0,
    });
  });

  it("快速切换场景时旧响应不覆盖所选场景，标签切回保留当前技能", async () => {
    useIframeStore.setState({ userId: "10086" });
    const plan = makeTodayPlan();
    useWealthStore.setState({
      plans: [
        {
          ...plan,
          items: [
            plan.items![0],
            { ...plan.items![0], id: FINANCE, sceneName: "理财场景" },
          ],
        },
      ],
    });
    let finish!: (response: unknown) => void;
    mockRequest.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const older = useWealthStore
      .getState()
      .loadTodayCustomers("business", LOAN);
    await useWealthStore.getState().loadTodayCustomers("business", FINANCE);
    finish({ items: NAME_LIST_FIXTURE });
    await older;
    expect(useWealthStore.getState()).toMatchObject({
      customers: [],
      todaySkillId: FINANCE,
      customersLoading: false,
    });
    planViews = [makeTodayPlanView()];
    planViews[0].scenes.push({
      ...planViews[0].scenes[0],
      sceneId: FINANCE,
      sceneName: "理财场景",
    });
    mockRequest.mockClear();
    await useWealthStore.getState().refreshTaskLists();
    const calls = mockRequest.mock.calls.filter(([path]) =>
      String(path).startsWith("/api/agent/workspace/name-list"),
    );
    expect(JSON.parse(String(calls[0][1]?.body)).skillIds).toEqual([FINANCE]);
  });

  it("经营视角只查询所选场景，其他模块和角标使用全部当天技能", async () => {
    useIframeStore.setState({ userId: "10086" });
    const plan = makeTodayPlan();
    useWealthStore.setState({
      plans: [
        {
          ...plan,
          items: [
            plan.items[0],
            { ...plan.items[0], id: FINANCE, sceneName: "理财场景" },
          ],
        },
      ],
    });
    mockRequest.mockClear();
    const state = useWealthStore.getState();
    await state.loadTodayCustomers("business", FINANCE);
    const skillArrays = () =>
      mockRequest.mock.calls
        .filter(([path]) =>
          String(path).startsWith("/api/agent/workspace/name-list"),
        )
        .map(([, options]) => JSON.parse(String(options?.body)).skillIds);
    expect(skillArrays()).toEqual([[FINANCE], [LOAN, FINANCE]]);
    mockRequest.mockClear();
    await state.loadTodayCustomers("customer");
    await state.loadPendingCustomers();
    await state.loadDoneCustomers();
    expect(skillArrays()).toEqual([
      [LOAN, FINANCE],
      [LOAN, FINANCE],
      [LOAN, FINANCE],
    ]);
  });

  beforeEach(async () => {
    await initStore();
  });

  it("init 加载工作台数据并推导真实身份账户；场景不进系统时预加载", () => {
    const s = useWealthStore.getState();
    // initStore 声明岗位 RB0101 → 客户经理，账户信息来自真实身份推导而非 mock 列表
    expect(selectCurrentAccount(s).role).toBe("客户经理");
    expect(selectCurrentAccount(s).source).toBe("我的关注");
    expect(s.scenesByCategory).toEqual({});
    expect(s.plans).toHaveLength(6);
    expect(s.draft).toEqual({ name: "", items: [] });
  });

  it("init 在规划请求完成前就完成身份初始化，允许工作台先渲染", async () => {
    let resolvePlans!: (value: { items: FixturePlanView[] }) => void;
    const pendingPlans = new Promise<{ items: FixturePlanView[] }>(
      (resolve) => {
        resolvePlans = resolve;
      },
    );
    mockRequest.mockImplementationOnce(() => pendingPlans as never);
    useWealthStore.setState({
      initialized: false,
      accountId: "unknown",
      plans: [],
    });

    const initialization = useWealthStore.getState().init();

    expect(useWealthStore.getState().initialized).toBe(true);
    expect(useWealthStore.getState().accountId).toBe("rm");
    expect(useWealthStore.getState().plans).toEqual([]);
    expect(useWealthStore.getState().plansLoaded).toBe(false);

    resolvePlans({ items: fixturePlanViews() });
    await initialization;
    expect(useWealthStore.getState().plans).toHaveLength(6);
    expect(useWealthStore.getState().plansLoaded).toBe(true);
  });

  it("loadScenes 按大类查询且每次都取最新；toggleScene 选择/移除场景", async () => {
    const sceneCalls = () =>
      mockRequest.mock.calls.filter(([p]) =>
        String(p).startsWith("/api/agent/workspace/skill-config/list"),
      ).length;
    await useWealthStore.getState().loadScenes("");
    await useWealthStore.getState().loadScenes(""); // 不做缓存，每次都重新查询
    expect(sceneCalls()).toBe(2);
    expect(
      useWealthStore.getState().scenesByCategory[""]?.map((s) => s.id),
    ).toEqual([INSURANCE, INSURANCE_2, FINANCE, LOAN]);

    const { toggleScene } = useWealthStore.getState();
    const ids = () => useWealthStore.getState().draft.items.map((x) => x.id);
    toggleScene(INSURANCE);
    expect(ids()).toEqual([INSURANCE]);
    // 选入草稿时携带场景技能的 cronExample（发布时作为定时任务请求内容）
    expect(useWealthStore.getState().draft.items[0]?.cronExample).toBe(
      "每日生成高潜保险客户名单",
    );
    toggleScene(INSURANCE_2);
    expect(ids()).toEqual([INSURANCE, INSURANCE_2]);
    toggleScene("skill-not-in-pool"); // 场景池外的 id 为无效操作
    expect(ids()).toEqual([INSURANCE, INSURANCE_2]);
    toggleScene(INSURANCE_2);
    expect(ids()).toEqual([INSURANCE]);
  });

  it("moveScene / dropScene 调整顺序", () => {
    seedDraft();
    const { moveScene, dropScene } = useWealthStore.getState();
    const ids = () => useWealthStore.getState().draft.items.map((x) => x.id);
    moveScene(0, 1);
    expect(ids()).toEqual([FINANCE, INSURANCE]);
    dropScene(FINANCE, INSURANCE);
    expect(ids()).toEqual([INSURANCE, FINANCE]);
  });

  it("setTaskCycle 预设周期自动带出起止日期", () => {
    useWealthStore.setState({ draft: { name: "x", items: [makeItem()] } });
    const { setTaskCycle } = useWealthStore.getState();
    setTaskCycle(INSURANCE, "本季");
    const item = useWealthStore
      .getState()
      .draft.items.find((x) => x.id === INSURANCE);
    const [start, end] = cycleRange("本季") ?? [];
    expect(item?.cycle).toBe("本季");
    expect(item?.start).toBe(start);
    expect(item?.end).toBe(end);
  });

  it("updateSchedule 修改场景排程", () => {
    useWealthStore.setState({ draft: { name: "x", items: [makeItem()] } });
    const { updateSchedule } = useWealthStore.getState();
    updateSchedule(INSURANCE, {
      type: "weekly",
      hour: 8,
      minute: 30,
      daysOfWeek: ["mon", "thu"],
    });
    const item = useWealthStore
      .getState()
      .draft.items.find((x) => x.id === INSURANCE);
    expect(item?.schedule.type).toBe("weekly");
    expect(item?.schedule.daysOfWeek).toEqual(["mon", "thu"]);
  });

  it("saveDraft 记录保存时间并弹出提示", async () => {
    await useWealthStore.getState().saveDraft();
    const s = useWealthStore.getState();
    expect(s.savedAt).toMatch(/^\d{2}:\d{2}$/);
    expect(s.toastText).toContain("草稿已保存");
  });

  it("validateDraft 校验空场景、空名称与非法日期", () => {
    expect(validateDraft({ name: "x", items: [] })).toBe(
      "请至少选择一个经营场景",
    );
    expect(validateDraft({ name: "  ", items: [makeItem()] })).toBe(
      "请填写规划名称",
    );
    expect(
      validateDraft({
        name: "x",
        items: [
          makeItem({
            cycle: "自定义",
            start: "2026-09-30",
            end: "2026-09-01",
          }),
        ],
      }),
    ).toContain("请设置");
  });

  it("validateDraft 校验每周执行日与自定义执行规则", () => {
    expect(
      validateDraft({
        name: "x",
        items: [makeItem({ schedule: { type: "weekly", hour: 9, minute: 0 } })],
      }),
    ).toBe("请选择「保障潜客经营」的每周执行日");
    expect(
      validateDraft({
        name: "x",
        items: [makeItem({ schedule: { type: "custom", rawCron: "abc" } })],
      }),
    ).toContain("自定义执行规则");
  });

  it("publishPlan 校验失败时不发请求并提示", async () => {
    useWealthStore.setState({ draft: { name: "x", items: [] } });
    const posts = () =>
      mockRequest.mock.calls.filter(
        ([, o]) => (o as RequestInit | undefined)?.method === "POST",
      ).length;
    const before = posts();
    const ok = await useWealthStore.getState().publishPlan();
    expect(ok).toBe(false);
    expect(posts()).toBe(before);
    expect(useWealthStore.getState().plans).toHaveLength(6);
    expect(useWealthStore.getState().toastText).toBe("请至少选择一个经营场景");
  });

  it("publishPlan 成功后更新列表并清除编辑态", async () => {
    seedDraft();
    const ok = await useWealthStore.getState().publishPlan();
    expect(ok).toBe(true);
    const s = useWealthStore.getState();
    expect(s.plans).toHaveLength(7);
    expect(s.editingId).toBeNull();
    expect(s.toastText).toContain("规划已发布");
    // 发布 payload 携带场景技能的 cronExample（后端组装定时任务请求内容用）
    const post = mockRequest.mock.calls.find(
      ([p, o]) =>
        String(p) === "/zt/wealth/plans" &&
        (o as RequestInit | undefined)?.method === "POST",
    );
    const body = JSON.parse(String((post?.[1] as RequestInit).body));
    expect(body.scenes[0].cronExample).toBe("每日生成高潜保险客户名单");
  });

  it("publishPlan 客户经理默认分发给自己", async () => {
    seedDraft();
    await useWealthStore.getState().publishPlan();
    expect(useWealthStore.getState().plans[0]?.targetSapIds).toEqual(["rm"]);
  });

  it("publishPlan 行长未选分发目标时校验失败", async () => {
    useWealthStore.setState({ accountId: "president" });
    seedDraft();
    const ok = await useWealthStore.getState().publishPlan();
    expect(ok).toBe(false);
    expect(useWealthStore.getState().plans).toHaveLength(6);
    expect(useWealthStore.getState().toastText).toBe("请至少选择一个分发目标");
  });

  it("publishPlan 行长选择分发目标后发布成功并记录 sapIds", async () => {
    useWealthStore.setState({ accountId: "president" });
    seedDraft();
    useWealthStore.getState().toggleTarget("zhangwl");
    useWealthStore.getState().toggleTarget("chenjy");
    const ok = await useWealthStore.getState().publishPlan();
    expect(ok).toBe(true);
    const s = useWealthStore.getState();
    expect(s.plans[0]?.targetSapIds).toEqual(["zhangwl", "chenjy"]);
    expect(s.targetSapIds).toEqual([]);
  });

  it("publishPlan 进行中重复触发只发一次请求", async () => {
    seedDraft();
    mockRequest.mockClear(); // 调用记录跨用例累积，只统计本用例内的请求
    const first = useWealthStore.getState().publishPlan();
    // 第一次调用尚未 await 完成时再触发，应被 acting 守卫直接拒绝
    const second = await useWealthStore.getState().publishPlan();
    expect(second).toBe(false);
    expect(await first).toBe(true);
    const posts = mockRequest.mock.calls.filter(
      ([p, o]) =>
        String(p) === "/zt/wealth/plans" &&
        (o as RequestInit | undefined)?.method === "POST",
    );
    expect(posts).toHaveLength(1);
    expect(useWealthStore.getState().acting).toBe(false); // finally 复位
  });

  it("removePlan 进行中重复触发只发一次请求", async () => {
    mockRequest.mockClear();
    const id = planViews[0]?.id ?? "";
    const first = useWealthStore.getState().removePlan(id);
    await useWealthStore.getState().removePlan(id); // 被守卫忽略
    await first;
    const dels = mockRequest.mock.calls.filter(
      ([p, o]) =>
        String(p).startsWith("/zt/wealth/plans/") &&
        (o as RequestInit | undefined)?.method === "DELETE",
    );
    expect(dels).toHaveLength(1);
    expect(useWealthStore.getState().acting).toBe(false);
  });

  it("toggleTarget 选中/取消分发目标", () => {
    useWealthStore.getState().toggleTarget("zhangwl");
    expect(useWealthStore.getState().targetSapIds).toEqual(["zhangwl"]);
    useWealthStore.getState().toggleTarget("zhangwl");
    expect(useWealthStore.getState().targetSapIds).toEqual([]);
  });

  it("loadTodayCustomers 两视角都带 sapId；经营视角按技能查，客户视角一次查全", async () => {
    useIframeStore.setState({ userId: "10086" });
    useWealthStore.setState({ plans: [makeTodayPlan()] });
    const nameListCalls = () =>
      mockRequest.mock.calls
        .filter(([p]) => String(p).startsWith("/api/agent/workspace/name-list"))
        .map(([, options]) => JSON.parse(String(options?.body)));

    await useWealthStore.getState().loadTodayCustomers("business");
    const bizCall = nameListCalls()[0];
    expect(bizCall.sapId).toBe("10086");
    expect(bizCall.skillIds).toEqual([LOAN]);
    const bizCustomers = useWealthStore.getState().customers;
    expect(bizCustomers).toHaveLength(2);
    expect(useWealthStore.getState().todayCustomerCount).toBe(2);
    expect(bizCustomers[0]).toMatchObject({
      id: `${LOAN}|CUST001`,
      task: "信贷需求挖掘",
      category: "贷款",
      reason: "命中贷款核验规则",
      done: false,
    });

    await useWealthStore.getState().loadTodayCustomers("customer");
    const custCall = nameListCalls().slice(-1)[0];
    expect(custCall.sapId).toBe("10086");
    expect(custCall.skillIds).toEqual([LOAN]);
    const custCustomers = useWealthStore.getState().customers;
    expect(custCustomers).toHaveLength(2);
    expect(useWealthStore.getState().todayCustomerCount).toBe(
      custCustomers.length,
    );
    expect(custCustomers[0]).toMatchObject({
      id: "CUST001",
      label: "我的关注",
      task: "信贷需求挖掘",
    });
  });

  it("侧栏总人数使用客户聚合查询，保留页面经营名单及视角", async () => {
    useIframeStore.setState({ userId: "10086" });
    const customers = [{ id: "business-1" }] as Customer[];
    useWealthStore.setState({
      plans: [makeTodayPlan()],
      customers,
      todayCustomerView: "business",
      todayCustomerCount: 0,
    });
    mockRequest.mockClear();
    await useWealthStore.getState().loadTodayCustomerCount();
    expect(mockRequest).toHaveBeenCalledWith(
      "/api/agent/workspace/name-list",
      expect.objectContaining({
        method: "POST",
      }),
    );
    const paths = mockRequest.mock.calls.map(([, options]) =>
      JSON.parse(String(options?.body)),
    );
    expect(paths.every((body) => body.skillIds.includes(LOAN))).toBe(true);
    expect(paths.some((body) => body.touched === 2)).toBe(true);
    expect(useWealthStore.getState().todayCustomerCount).toBe(2);
    expect(useWealthStore.getState().customers).toBe(customers);
    expect(useWealthStore.getState().todayCustomerView).toBe("business");
  });

  it("经营视角名单为空时，角标仍使用客户视角查询人数", async () => {
    useIframeStore.setState({ userId: "10086" });
    const plan = makeTodayPlan();
    useWealthStore.setState({
      plans: [
        { ...plan, items: [{ ...plan.items[0], id: FINANCE }, plan.items[0]] },
      ],
      todayCustomerCount: 0,
    });
    await useWealthStore.getState().loadTodayCustomers("business");
    expect(useWealthStore.getState().customers).toHaveLength(0);
    expect(useWealthStore.getState().todayCustomerCount).toBe(2);
  });

  it("刷新先等待最新规划，再用新技能查询名单，期间拦截重复刷新", async () => {
    const day = todayKey();
    const latest = makeView({
      periodStart: day,
      periodEnd: day,
      scenes: [
        {
          sceneId: LOAN,
          sceneName: "新发布的信贷任务",
          category: "loan",
          categoryLabel: "贷款",
          startDate: day,
          endDate: day,
          cronExpr: "0 9 * * *",
          mcpRelations: [],
        },
      ],
    });
    useWealthStore.setState({ plans: [], customersLoading: false });
    let finishPlans!: (value: unknown) => void;
    mockRequest.mockClear();
    mockRequest.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishPlans = resolve;
        }),
    );
    const refresh = useWealthStore.getState().refreshTodayCustomers("business");
    expect(useWealthStore.getState().customersLoading).toBe(true);
    expect(mockRequest.mock.calls.map(([path]) => path)).toEqual([
      "/zt/wealth/plans",
    ]);
    await useWealthStore.getState().refreshTodayCustomers("business");
    expect(mockRequest).toHaveBeenCalledTimes(1);
    finishPlans({ items: [latest] });
    await refresh;
    expect(
      JSON.parse(String(mockRequest.mock.calls[1]?.[1]?.body)).skillIds,
    ).toEqual([LOAN]);
    expect(useWealthStore.getState().customers[0].task).toBe(
      "新发布的信贷任务",
    );
    expect(useWealthStore.getState().customersLoading).toBe(false);
  });

  it("客户视角刷新规划后只查询一次聚合名单", async () => {
    planViews = [makeTodayPlanView()];
    useWealthStore.setState({ customersLoading: false });
    mockRequest.mockClear();
    await useWealthStore.getState().refreshTodayCustomers("customer");
    expect(mockRequest.mock.calls).toHaveLength(2);
    expect(mockRequest.mock.calls[0][0]).toBe("/zt/wealth/plans");
    expect(String(mockRequest.mock.calls[1][0])).toContain(
      "/api/agent/workspace/name-list",
    );
    expect(
      JSON.parse(String(mockRequest.mock.calls[1][1]?.body)).skillIds,
    ).toEqual([LOAN]);
    expect(useWealthStore.getState().customersLoading).toBe(false);
  });

  it("无任务权限时刷新不发送请求", async () => {
    useWealthStore.setState({
      accountId: "president",
      customersLoading: false,
    });
    mockRequest.mockClear();
    await useWealthStore.getState().refreshTodayCustomers("business");
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("loadPendingCustomers / loadDoneCustomers 按各自触达参数查询名单", async () => {
    useIframeStore.setState({ userId: "10086" });
    useWealthStore.setState({ plans: [makeTodayPlan()] });
    const nameListCalls = () =>
      mockRequest.mock.calls
        .filter(([p]) => String(p).startsWith("/api/agent/workspace/name-list"))
        .map(([, options]) => JSON.parse(String(options?.body)));

    mockRequest.mockResolvedValueOnce({
      items: [{ ...NAME_LIST_FIXTURE[0], touched: 0 }],
    });
    await useWealthStore.getState().loadPendingCustomers();
    mockRequest.mockResolvedValueOnce({
      items: [{ ...NAME_LIST_FIXTURE[1], touched: 1 }],
    });
    await useWealthStore.getState().loadDoneCustomers();

    const calls = nameListCalls();
    expect(calls).toHaveLength(2);
    expect(calls[0].touched).toBe(0);
    expect(calls[1].touched).toBe(1);
    expect(useWealthStore.getState().pendingCustomers).toHaveLength(1);
    expect(useWealthStore.getState().pendingCustomers[0].touched).toBe(0);
    const done = useWealthStore.getState().doneCustomers;
    expect(done).toHaveLength(1);
    expect(done[0].touched).toBe(1);
    expect(done.every((c) => c.done)).toBe(true);
  });

  it("标签页切回直接刷新三个名单，沿用今日客户视角和登录人", async () => {
    planViews = [makeTodayPlanView()];
    useWealthStore.setState({ plans: [makeTodayPlan()] });
    useIframeStore.setState({ userId: "10086" });
    await useWealthStore.getState().loadTodayCustomers("customer");
    mockRequest.mockClear();
    await useWealthStore.getState().refreshTaskLists();
    const paths = mockRequest.mock.calls.map(([path]) => String(path));
    expect(paths[0]).toBe("/zt/wealth/plans");
    const lists = mockRequest.mock.calls
      .filter(([path]) =>
        String(path).startsWith("/api/agent/workspace/name-list"),
      )
      .map(([, options]) => JSON.parse(String(options?.body)));
    expect(lists).toHaveLength(3);
    expect(lists.map((body) => body.touched)).toEqual([2, 0, 1]);
    expect(lists.every((body) => body.sapId === "10086")).toBe(true);
    expect(lists.every((body) => body.skillIds.includes(LOAN))).toBe(true);
    expect(useWealthStore.getState().taskListsRefreshing).toBe(false);
  });

  it("三个名单刷新期间忽略重复请求，完成后可以再次刷新", async () => {
    planViews = [makeTodayPlanView()];
    useWealthStore.setState({
      todayCustomerView: "customer",
      todayCustomerCount: 0,
    });
    let finishPlans!: (value: unknown) => void;
    mockRequest.mockClear();
    mockRequest.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishPlans = resolve;
        }),
    );
    const refresh = useWealthStore.getState().refreshTaskLists();
    await useWealthStore.getState().refreshTaskLists();
    expect(mockRequest).toHaveBeenCalledTimes(1);
    finishPlans({ items: planViews });
    await refresh;
    expect(mockRequest).toHaveBeenCalledTimes(4);
    await useWealthStore.getState().refreshTaskLists();
    expect(mockRequest).toHaveBeenCalledTimes(8);
  });

  it("人数相同也刷新客户明细，不再先查询数量", async () => {
    planViews = [makeTodayPlanView()];
    useWealthStore.setState({ plans: [makeTodayPlan()] });
    await useWealthStore.getState().loadTodayCustomers("customer");
    const originalCount = useWealthStore.getState().todayCustomerCount;
    const updated = NAME_LIST_FIXTURE.map((item, index) => ({
      ...item,
      custNm: `最新客户${index}`,
      touched: index,
    }));
    mockRequest.mockClear();
    mockRequest.mockResolvedValueOnce({ items: planViews });
    mockRequest.mockResolvedValueOnce({ items: updated });
    mockRequest.mockResolvedValueOnce({ items: [updated[0]] });
    mockRequest.mockResolvedValueOnce({ items: [updated[1]] });
    await useWealthStore.getState().refreshTaskLists();
    expect(mockRequest).toHaveBeenCalledTimes(4);
    expect(mockRequest.mock.calls[0][0]).toBe("/zt/wealth/plans");
    expect(useWealthStore.getState().todayCustomerCount).toBe(originalCount);
    expect(useWealthStore.getState().customers[0].name).toBe("最新客户0");
    expect(useWealthStore.getState().pendingCustomers[0].name).toBe(
      "最新客户0",
    );
    expect(useWealthStore.getState().doneCustomers[0].name).toBe("最新客户1");
    expect(useWealthStore.getState().taskListsRefreshing).toBe(false);
  });

  it.each([
    { initialized: false },
    { plansLoaded: false },
    { accountId: "president" as const },
    { accountId: "unknown" as const },
    { customersLoading: true },
    { pendingLoading: true },
    { doneLoading: true },
  ])("身份或初始规划未就绪、无任务权限时不刷新：%j", async (state) => {
    useWealthStore.setState(state);
    mockRequest.mockClear();
    await useWealthStore.getState().refreshTaskLists();
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("editPlan 将规划内容载入草稿并设置编辑态", () => {
    useWealthStore.getState().editPlan("plan-demo-2");
    const s = useWealthStore.getState();
    expect(s.editingId).toBe("plan-demo-2");
    expect(s.draft.name).toBe("产品到期承接");
    expect(s.draft.items.length).toBeGreaterThan(0);
  });

  it("removePlan 移除规划并提示", async () => {
    await useWealthStore.getState().removePlan("plan-demo-3");
    const s = useWealthStore.getState();
    expect(s.plans.some((p) => p.id === "plan-demo-3")).toBe(false);
    expect(s.toastText).toBe("规划已移除");
  });

  it("planScheduledOn 按排程推算（weekly 按执行日匹配）", () => {
    const plan: Plan = {
      id: "plan-x",
      name: "",
      source: "",
      desc: "",
      customers: 0,
      tasks: 0,
      rate: 0,
      status: "",
      period: "",
      start: "2026-09-01",
      end: "2026-09-30",
      items: [
        makeItem({
          cycle: "自定义",
          schedule: {
            type: "weekly",
            hour: 9,
            minute: 0,
            daysOfWeek: ["mon", "thu"],
          },
        }),
      ],
    };
    expect(planScheduledOn(plan, "2026-09-07")).toBe(true); // 周一
    expect(planScheduledOn(plan, "2026-09-08")).toBe(false); // 周二
    expect(planScheduledOn(plan, "2026-09-10")).toBe(true); // 周四
    expect(planScheduledOn(plan, "2026-10-01")).toBe(false); // 超出周期
  });

  it("planScheduledOn 非 weekly 排程在周期内每日可见", () => {
    const plan: Plan = {
      id: "plan-y",
      name: "",
      source: "",
      desc: "",
      customers: 0,
      tasks: 0,
      rate: 0,
      status: "",
      period: "",
      start: "2026-09-01",
      end: "2026-09-30",
      items: [makeItem({ cycle: "自定义" })],
    };
    expect(planScheduledOn(plan, "2026-09-08")).toBe(true);
    expect(planScheduledOn(plan, "2026-08-31")).toBe(false);
  });
  it("看板严格刷新失败时保留已有规划", async () => {
    const previous = useWealthStore.getState().plans;
    mockRequest.mockRejectedValue(new Error("offline"));
    await expect(useWealthStore.getState().refreshPlans(true)).rejects.toThrow(
      "offline",
    );
    expect(useWealthStore.getState().plans).toBe(previous);
  });
  it("被取消的规划请求即使晚返回也不覆盖新规划", async () => {
    let resolve!: (value: unknown) => void;
    mockRequest.mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const controller = new AbortController();
    const refresh = useWealthStore
      .getState()
      .refreshPlans(true, controller.signal);
    const rejection = expect(refresh).rejects.toThrow();
    controller.abort();
    const latest = [...useWealthStore.getState().plans];
    useWealthStore.setState({ plans: latest });
    resolve({ items: [] });
    await rejection;
    expect(useWealthStore.getState().plans).toBe(latest);
    expect(mockRequest).toHaveBeenCalledWith(
      "/zt/wealth/plans",
      expect.objectContaining({
        signal: controller.signal,
      }),
    );
  });
});
