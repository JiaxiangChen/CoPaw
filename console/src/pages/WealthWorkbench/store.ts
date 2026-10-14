/**
 * 智能财富工作台 —— zustand store
 *
 * 只承载跨页共享的领域数据（账户 / 场景池 / 规划 / 客户 / 草稿）与全局 UI（弹窗、Toast）。
 * 页面内视图状态（筛选、分页、日历锚点等）留在组件内 useState。
 * 规划与场景经由 api.ts 的真实接口（/wealth/plans、/wealth/scene-skills），
 * 接口不可达时读路径返回空、写路径抛错提示，不做假数据兜底。
 */
import { create } from "zustand";
import type { ReactNode } from "react";
import type { CronParts } from "@/utils/parseCron";
import { useIframeStore } from "../../stores/iframeStore";
import * as api from "./api";
import {
  canAccessPage,
  FALLBACK_ROLE,
  needsDistributeTargets,
  resolveRole,
  ROLE_ACCOUNT_META,
  type WealthPage,
  type WealthRole,
} from "./permissions";
import {
  fetchDistributeTargets,
  type DistributeBranch,
} from "./distributeTargets";
import {
  buildTaskTree,
  CYCLE_LABELS,
  cycleRange,
  DEFAULT_SCHEDULE,
  todayKey,
  validCronExpr,
  validTaskDate,
} from "./utils";
import type {
  Account,
  Customer,
  DistributeTarget,
  Draft,
  Plan,
  PlanItem,
  Scene,
} from "./types";

export interface DialogButton {
  label: string;
  primary?: boolean;
  danger?: boolean;
  /** 置灰不可点击（用于前置校验未通过的场景，原因在弹窗正文说明） */
  disabled?: boolean;
  /** 缺省时点击仅关闭弹窗 */
  onClick?: () => void;
}

export interface DialogState {
  /** 弹窗标题；传空串或省略时不渲染标题头部（如经营方案 iframe 预览） */
  title?: string;
  body: ReactNode;
  buttons: DialogButton[];
  /** 宽弹窗（工作任务详情日历表） */
  wide?: boolean;
  /**
   * 无边框内容弹窗：隐去标题头部、正文区域不留内边距，
   * 让内容（如 iframe）铺满整个弹窗（对应经营方案预览）。
   */
  plain?: boolean;
}

interface WealthState {
  initialized: boolean;
  /** 生效角色：由父系统 positionId 经 resolveRole 解析（见 permissions.ts） */
  accountId: WealthRole;
  /** 按大类缓存的经营场景（/wealth/scene-skills）；键为大类英文 code，空串表示全部 */
  scenesByCategory: Record<string, Scene[]>;
  /** 场景查询进行中（按分类点击懒加载） */
  scenesLoading: boolean;
  plans: Plan[];
  /** 规划基础数据已完成首次加载（失败时也会以空列表完成） */
  plansLoaded: boolean;
  customers: Customer[];
  todayCounts: api.CustomerCounts | null;
  /** 今日任务客户视角聚合名单的总人数，独立于当前页面视角。 */
  todayCustomerCount: number;
  /** 客户名单查询中（今日任务视角切换时重新拉取） */
  customersLoading: boolean;
  /** 最近查询的今日任务视角，用于标签页切回时保持名单口径。 */
  todayCustomerView: "business" | "customer";
  /** 最近查询的经营场景，用于刷新时保持当前选择。 */
  todaySkillId: string;
  taskListsRefreshing: boolean;
  /** 待触达客户名单（/wealth/name-list，touched=0） */
  pendingCustomers: Customer[];
  pendingCounts: api.CustomerCounts | null;
  pendingLoading: boolean;
  /** 已完成客户名单（/wealth/name-list，touched=1） */
  doneCustomers: Customer[];
  doneCounts: api.CustomerCounts | null;
  doneLoading: boolean;
  draft: Draft;
  savedAt: string;
  editingId: string | null;
  /** 分发目标用户池（仅行长/中台加载） */
  targets: DistributeTarget[];
  targetsLoaded: boolean;
  /** 中台当前浏览的支行；targets 保留跨支行人员信息用于发布。 */
  targetBranch: DistributeBranch | null;
  branchTargetSapIds: string[];
  /** 已选中的分发目标 sapId 列表 */
  targetSapIds: string[];
  dialog: DialogState | null;
  /** 写操作进行中（发布/移除）：弹窗按钮禁用并防重复提交 */
  acting: boolean;
  toastText: string;
  toastSeq: number;

  init: () => Promise<void>;
  /** 看板轮询：有规划处于发布中时刷新列表 */
  refreshPlans: (throwOnError?: boolean, signal?: AbortSignal) => Promise<void>;
  /**
   * 拉取今日任务的客户经营清单（/wealth/name-list）。
   * 两个视角都传 sapId 与技能数组；经营视角仅当前场景，其他口径为当天全部。
   */
  loadTodayCustomers: (
    view: "business" | "customer",
    skillId?: string,
  ) => Promise<void>;
  loadTodayCustomerCount: () => Promise<void>;
  refreshTodayCustomers: (
    view: "business" | "customer",
    skillId?: string,
  ) => Promise<void>;
  refreshTaskLists: () => Promise<void>;
  /** 加载待触达客户名单（仅客户经理；touched=0） */
  loadPendingCustomers: () => Promise<void>;
  /** 加载已完成客户名单（仅客户经理；touched=1） */
  loadDoneCustomers: () => Promise<void>;

  // —— 分发目标（行长/中台） ——
  loadTargets: (branch?: DistributeBranch) => Promise<void>;
  resetTargetBranch: () => void;
  toggleTarget: (sapId: string) => void;

  // —— 经营场景（按分类点击查询，每次都取最新数据） ——
  loadScenes: (categoryCode: string) => Promise<void>;

  // —— 草稿编辑（纯内存） ——
  setDraftName: (name: string) => void;
  toggleScene: (id: string) => void;
  moveScene: (index: number, delta: number) => void;
  dropScene: (dragId: string, targetId: string) => void;
  setTaskCycle: (id: string, cycle: string) => void;
  setItemDates: (id: string, field: "start" | "end", value: string) => void;
  updateSchedule: (id: string, patch: Partial<CronParts>) => void;
  setItemDirection: (id: string, direction: string) => void;
  normalizeDraft: () => void;
  saveDraft: () => Promise<void>;

  // —— 规划生命周期 ——
  editPlan: (id: string) => void;
  clearEditingId: () => void;
  publishPlan: () => Promise<boolean>;
  removePlan: (id: string) => Promise<void>;

  // —— 全局 UI ——
  openDialog: (dialog: DialogState) => void;
  closeDialog: () => void;
  toast: (text: string) => void;
}

/**
 * 由生效角色推导当前账户：角色元数据（显示名/来源标签）来自 ROLE_ACCOUNT_META，
 * 姓名与机构取 iframeStore 的真实身份（父系统 cookie 链路），无任何 mock。
 *
 * 该函数同时是 useWealthStore 的 selector（见 selectCurrentAccount），
 * zustand 以 Object.is 比较结果，因此按入参缓存、输入不变时返回同一引用，
 * 避免每次渲染生成新对象导致的无限重渲染。
 */
let accountCacheKey = "";
let accountCache: Account | null = null;

function accountForRole(role: WealthRole): Account {
  const { userName, orgCode } = useIframeStore.getState();
  const key = `${role}|${userName ?? ""}|${orgCode ?? ""}`;
  if (accountCache && accountCacheKey === key) {
    return accountCache;
  }
  const meta = ROLE_ACCOUNT_META[role];
  accountCacheKey = key;
  accountCache = {
    id: role,
    name: userName ?? "",
    role: meta.role,
    department: orgCode ?? "",
    source: meta.source,
  };
  return accountCache;
}

function currentAccountOf(state: { accountId: WealthRole }): Account {
  return accountForRole(state.accountId);
}

/** 今日有排程的经营场景引用（客户名单查询的入参上下文：skillId → 场景名映射） */
function todayTaskRefs(plans: Plan[]): api.TodayTaskRef[] {
  return buildTaskTree(plans, todayKey()).flatMap((g) =>
    g.nodes.map((n) => ({
      skillId: n.sceneId,
      sceneName: n.sceneName,
      category: g.category,
      source: n.source,
    })),
  );
}

/** 与原型 normalizeDraft 一致：补齐周期区间与排程 */
function normalizeItems(draft: Draft): Draft {
  const items = draft.items.map((x) => {
    const cycle = CYCLE_LABELS.includes(x.cycle) ? x.cycle : "自定义";
    const range = cycleRange(cycle) ?? cycleRange("本月");
    return {
      ...x,
      cycle,
      start: x.start ?? range?.[0],
      end: x.end ?? range?.[1],
      schedule: x.schedule ?? { ...DEFAULT_SCHEDULE },
    };
  });
  return { ...draft, items };
}

/** 校验单个场景的排程，返回错误文案；通过时返回 null */
function validateSchedule(item: PlanItem): string | null {
  const schedule = item.schedule ?? DEFAULT_SCHEDULE;
  if (schedule.type === "weekly" && !schedule.daysOfWeek?.length) {
    return `请选择「${item.sceneName}」的每周执行日`;
  }
  if (schedule.type === "custom" && !validCronExpr(schedule.rawCron)) {
    return `请重新配置「${item.sceneName}」的自定义执行规则`;
  }
  return null;
}

/** 校验草稿，返回第一条错误文案；通过时返回 null */
export function validateDraft(draft: Draft): string | null {
  const normalized = normalizeItems(draft);
  if (!normalized.items.length) return "请至少选择一个经营场景";
  if (!normalized.name.trim()) return "请填写规划名称";
  if (normalized.items.some((x) => !x.direction.trim()))
    return "请填写每个场景的经营方向";
  const invalid = normalized.items.find(
    (x) =>
      !validTaskDate(x.start) || !validTaskDate(x.end) || x.end! < x.start!,
  );
  if (invalid) {
    return `请设置「${invalid.sceneName}」的正确任务时间区间`;
  }
  for (const item of normalized.items) {
    const error = validateSchedule(item);
    if (error) return error;
  }
  return null;
}

export {
  calendarDate,
  dateKey,
  planFrequency,
  planScheduledOn,
  scheduleLabel,
  todayKey,
  validTaskDate,
} from "./utils";

let targetRequestSeq = 0;
let todayCustomersRequestSeq = 0;

export const useWealthStore = create<WealthState>()((set, get) => ({
  initialized: false,
  accountId: "rm",
  scenesByCategory: {},
  scenesLoading: false,
  plans: [],
  plansLoaded: false,
  customers: [],
  todayCounts: null,
  todayCustomerCount: 0,
  customersLoading: false,
  todayCustomerView: "business",
  todaySkillId: "",
  taskListsRefreshing: false,
  pendingCustomers: [],
  pendingCounts: null,
  pendingLoading: false,
  doneCustomers: [],
  doneCounts: null,
  doneLoading: false,
  draft: { name: "", items: [] },
  savedAt: "",
  editingId: null,
  targets: [],
  targetsLoaded: false,
  targetBranch: null,
  branchTargetSapIds: [],
  targetSapIds: [],
  dialog: null,
  acting: false,
  toastText: "",
  toastSeq: 0,

  init: async () => {
    if (get().initialized) return;
    // 生效角色由父系统 positionId 解析；未识别身份（unknown）整页拦截，不加载业务数据
    const accountId = resolveRole(useIframeStore.getState().positionId);
    if (accountId === FALLBACK_ROLE) {
      set({ initialized: true, accountId, plansLoaded: true });
      return;
    }
    // 身份已经解析完成即可渲染工作台框架；规划数据在后台加载并随后填充。
    set({ initialized: true, accountId, plansLoaded: false });
    const data = await api.fetchBootstrap(accountId);
    set({ ...data, plansLoaded: true });
  },

  refreshPlans: async (throwOnError = false, signal) => {
    const plans = await api.fetchPlanList(throwOnError, signal);
    signal?.throwIfAborted();
    set({ plans });
  },

  refreshTodayCustomers: async (view, skillId) => {
    const state = get();
    if (!canAccessPage(state.accountId, "tasks") || state.customersLoading)
      return;
    const requestSeq = ++todayCustomersRequestSeq;
    set({ customersLoading: true });
    try {
      await get().refreshPlans();
      if (requestSeq !== todayCustomersRequestSeq) return;
      await get().loadTodayCustomers(view, skillId ?? state.todaySkillId);
    } catch (error) {
      get().toast(
        error instanceof Error
          ? `刷新失败：${error.message}`
          : "刷新失败，请重试",
      );
    } finally {
      if (requestSeq === todayCustomersRequestSeq)
        set({ customersLoading: false });
    }
  },

  refreshTaskLists: async () => {
    const state = get();
    if (
      !state.initialized ||
      !state.plansLoaded ||
      !canAccessPage(state.accountId, "tasks") ||
      state.taskListsRefreshing ||
      state.customersLoading ||
      state.pendingLoading ||
      state.doneLoading
    )
      return;
    set({ taskListsRefreshing: true });
    try {
      await get().refreshTodayCustomers(state.todayCustomerView);
      const results = await Promise.allSettled([
        get().pendingLoading ? Promise.resolve() : get().loadPendingCustomers(),
        get().doneLoading ? Promise.resolve() : get().loadDoneCustomers(),
      ]);
      if (results.some((result) => result.status === "rejected")) {
        get().toast("任务名单刷新失败，请重试");
      }
    } finally {
      set({ taskListsRefreshing: false });
    }
  },

  loadTodayCustomers: async (view, skillId) => {
    const { accountId, plans } = get();
    // 仅客户经理有任务页；行长/中台不发名单查询
    if (!canAccessPage(accountId, "tasks")) return;
    const sapId = useIframeStore.getState().userId || undefined;
    const tasks = todayTaskRefs(plans);
    const selected =
      tasks.find((task) => task.skillId === (skillId ?? get().todaySkillId)) ??
      tasks[0];
    const queryTasks =
      view === "business" ? (selected ? [selected] : []) : tasks;
    const requestSeq = ++todayCustomersRequestSeq;
    set({
      customersLoading: true,
      todayCustomerView: view,
      ...(view === "business" ? { todaySkillId: selected?.skillId ?? "" } : {}),
    });
    try {
      const [result, customerViewResult] = await Promise.all([
        api.fetchTodayCustomers(queryTasks, sapId, view),
        view === "business"
          ? api.fetchTodayCustomers(tasks, sapId, "customer")
          : Promise.resolve(null),
      ]);
      if (requestSeq !== todayCustomersRequestSeq) return;
      set({
        customers: result.customers,
        todayCounts: result.counts,
        todayCustomerCount: (customerViewResult ?? result).customers.length,
      });
    } finally {
      if (requestSeq === todayCustomersRequestSeq)
        set({ customersLoading: false });
    }
  },

  loadTodayCustomerCount: async () => {
    const { accountId, plans } = get();
    if (!canAccessPage(accountId, "tasks")) return;
    const sapId = useIframeStore.getState().userId || undefined;
    const result = await api.fetchTodayCustomers(
      todayTaskRefs(plans),
      sapId,
      "customer",
    );
    set({ todayCustomerCount: result.customers.length });
  },

  loadPendingCustomers: async () => {
    const { accountId, plans } = get();
    if (!canAccessPage(accountId, "tasks")) return;
    set({ pendingLoading: true });
    const sapId = useIframeStore.getState().userId || undefined;
    const result = await api.fetchPendingCustomers(
      todayTaskRefs(plans),
      sapId,
    );
    set({
      pendingCustomers: result.customers,
      pendingCounts: result.counts,
      pendingLoading: false,
    });
  },

  loadDoneCustomers: async () => {
    const { accountId, plans } = get();
    if (!canAccessPage(accountId, "tasks")) return;
    set({ doneLoading: true });
    const sapId = useIframeStore.getState().userId || undefined;
    const result = await api.fetchDoneCustomers(
      todayTaskRefs(plans),
      sapId,
    );
    set({
      doneCustomers: result.customers,
      doneCounts: result.counts,
      doneLoading: false,
    });
  },

  resetTargetBranch: () => {
    targetRequestSeq += 1;
    set({ targetBranch: null, branchTargetSapIds: [], targetsLoaded: false });
  },

  loadTargets: async (branch) => {
    if (get().accountId === "middle" && !branch) return;
    if (!branch && get().targetsLoaded) return;
    const requestSeq = ++targetRequestSeq;
    set({
      targetsLoaded: false,
      targetBranch: branch ?? null,
      branchTargetSapIds: [],
    });
    const targets = await fetchDistributeTargets(branch);
    if (requestSeq !== targetRequestSeq) return;
    set((state) => ({
      targets:
        branch || state.editingId
          ? Array.from(
              new Map(
                [...state.targets, ...targets].map((t) => [t.sapId, t]),
              ).values(),
            )
          : targets,
      branchTargetSapIds: targets.map((t) => t.sapId),
      targetsLoaded: true,
    }));
  },

  toggleTarget: (sapId) => {
    const { targetSapIds } = get();
    set({
      targetSapIds: targetSapIds.includes(sapId)
        ? targetSapIds.filter((x) => x !== sapId)
        : [...targetSapIds, sapId],
    });
  },

  loadScenes: async (categoryCode) => {
    // 每次点击分类都重新查询最新数据；按大类分键存储，
    // 快速切换标签时各响应只写自己的 key，天然免疫乱序覆盖
    set({ scenesLoading: true });
    const scenes = await api.fetchScenesByCategory(categoryCode);
    set((s) => ({
      scenesByCategory: { ...s.scenesByCategory, [categoryCode]: scenes },
      scenesLoading: false,
    }));
  },

  setDraftName: (name) => set((s) => ({ draft: { ...s.draft, name } })),

  toggleScene: (id) => {
    const { draft, scenesByCategory } = get();
    const index = draft.items.findIndex((x) => x.id === id);
    if (index < 0) {
      const scene = Object.values(scenesByCategory)
        .flat()
        .find((s) => s.id === id);
      if (!scene) return;
      const range = cycleRange("本月");
      set({
        draft: {
          ...draft,
          items: [
            ...draft.items,
            {
              id,
              sceneName: scene.name,
              categoryLabel: scene.category,
              categoryCode: scene.categoryCode,
              itemId: scene.itemId,
              cronExample: scene.cronExample,
              mcpRelations: scene.mcpRelations,
              direction: scene.desc,
              cycle: "本月",
              start: range?.[0],
              end: range?.[1],
              schedule: { ...DEFAULT_SCHEDULE },
            },
          ],
        },
      });
    } else {
      set({
        draft: {
          ...draft,
          items: draft.items.filter((x) => x.id !== id),
        },
      });
    }
  },

  moveScene: (index, delta) => {
    const { draft } = get();
    const target = index + delta;
    if (target < 0 || target >= draft.items.length) return;
    const items = [...draft.items];
    [items[index], items[target]] = [items[target], items[index]];
    set({ draft: { ...draft, items } });
  },

  dropScene: (dragId, targetId) => {
    const { draft } = get();
    const from = draft.items.findIndex((x) => x.id === dragId);
    const to = draft.items.findIndex((x) => x.id === targetId);
    if (from < 0 || to < 0) return;
    const items = [...draft.items];
    items.splice(to, 0, items.splice(from, 1)[0]);
    set({ draft: { ...draft, items } });
  },

  setTaskCycle: (id, cycle) => {
    const { draft } = get();
    const items: PlanItem[] = draft.items.map((x) => {
      if (x.id !== id) return x;
      const range = cycleRange(cycle);
      return range
        ? { ...x, cycle, start: range[0], end: range[1] }
        : { ...x, cycle };
    });
    set({ draft: { ...draft, items } });
  },

  setItemDates: (id, field, value) => {
    const { draft } = get();
    set({
      draft: {
        ...draft,
        items: draft.items.map((x) =>
          x.id === id ? { ...x, [field]: value } : x,
        ),
      },
    });
  },

  updateSchedule: (id, patch) => {
    const { draft } = get();
    set({
      draft: {
        ...draft,
        items: draft.items.map((x) =>
          x.id === id
            ? {
                ...x,
                schedule: { ...(x.schedule ?? DEFAULT_SCHEDULE), ...patch },
              }
            : x,
        ),
      },
    });
  },

  setItemDirection: (id, direction) => {
    const { draft } = get();
    set({
      draft: {
        ...draft,
        items: draft.items.map((x) => (x.id === id ? { ...x, direction } : x)),
      },
    });
  },

  normalizeDraft: () => {
    set((s) => ({ draft: normalizeItems(s.draft) }));
  },

  saveDraft: async () => {
    const state = get();
    const draft = normalizeItems(state.draft);
    const { savedAt } = await api.saveDraft(state.accountId, draft);
    set({ draft, savedAt });
    get().toast("草稿已保存，可在当前会话继续编辑");
  },

  editPlan: (id) => {
    const p = get().plans.find((x) => x.id === id);
    if (!p) return;
    const items = p.items ? JSON.parse(JSON.stringify(p.items)) : [];
    const draft = normalizeItems({ name: p.name, items });
    const targetSapIds = p.targetSapIds ?? [];
    const targets = targetSapIds.map(
      (sapId) =>
        p.targets?.find((target) => target.sapId === sapId) ?? {
          sapId,
          name: sapId,
          orgName: "",
        },
    );
    targetRequestSeq += 1;
    set({
      draft,
      editingId: id,
      targetSapIds,
      targets,
      targetsLoaded: false,
      targetBranch: null,
      branchTargetSapIds: [],
    });
  },

  /** 离开创建页时清除编辑态（与原型 navigate() 行为一致：草稿本身保留） */
  clearEditingId: () => {
    if (get().editingId != null) set({ editingId: null });
  },

  publishPlan: async () => {
    const state = get();
    const error = validateDraft(state.draft);
    if (error) {
      get().toast(error);
      return false;
    }
    const account = currentAccountOf(state);
    // 客户经理的规划只发给自己；行长/中台必须至少选择一个分发目标
    const targets = buildPublishTargets(state, account);
    if (needsDistributeTargets(account.id) && !targets.length) {
      get().toast("请至少选择一个分发目标");
      return false;
    }
    if (get().acting) return false; // 防重复提交
    set({ acting: true });
    const draft = normalizeItems(state.draft);
    try {
      const { plans, created } = await api.publishPlan(
        account,
        draft,
        state.editingId,
        targets,
      );
      set({
        plans,
        draft,
        editingId: null,
        dialog: null,
        savedAt: "",
        targetSapIds: [],
      });
      get().toast(
        created
          ? "规划已发布，正在后台创建定时任务并分发"
          : "规划已修改，正在重新发布",
      );
      return true;
    } catch (error) {
      // HTTP 业务错误（409 发布中 / 403 非创建人 / 认证失效等）原样提示
      get().toast(
        error instanceof Error ? `发布失败：${error.message}` : "发布失败",
      );
      return false;
    } finally {
      set({ acting: false });
    }
  },

  removePlan: async (id) => {
    if (get().acting) return; // 防重复提交
    set({ acting: true });
    try {
      const { plans } = await api.removePlan(id);
      set({ plans, dialog: null });
      get().toast("规划已移除");
    } catch (error) {
      get().toast(
        error instanceof Error ? `移除失败：${error.message}` : "移除失败",
      );
    } finally {
      set({ acting: false });
    }
  },

  openDialog: (dialog) => set({ dialog }),
  closeDialog: () => set({ dialog: null }),

  toast: (text) => set((s) => ({ toastText: text, toastSeq: s.toastSeq + 1 })),
}));

/** 组装发布入参的分发目标（含姓名冗余）；客户经理默认发给自己 */
function buildPublishTargets(
  state: Pick<WealthState, "targets" | "targetSapIds" | "accountId">,
  account: Account,
): DistributeTarget[] {
  if (!needsDistributeTargets(account.id)) {
    const sapId = useIframeStore.getState().userId ?? account.id;
    return [{ sapId, name: account.name, orgName: account.department }];
  }
  return state.targetSapIds.map((sapId) => {
    const hit = state.targets.find((t) => t.sapId === sapId);
    return {
      sapId,
      name: hit?.name ?? sapId,
      orgName: hit?.orgName ?? "",
    };
  });
}

/** 当前账户（角色元数据 + iframeStore 真实身份推导）；组件内配合 useWealthStore 使用 */
export function selectCurrentAccount(s: WealthState): Account {
  return accountForRole(s.accountId);
}

/** 当前生效角色是否可访问某页面组；权限唯一判定入口（矩阵见 permissions.ts） */
export function useCanAccess(page: WealthPage): boolean {
  return useWealthStore((s) => canAccessPage(s.accountId, page));
}
