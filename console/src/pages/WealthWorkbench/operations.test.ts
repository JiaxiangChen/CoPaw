import { beforeEach, describe, expect, it, vi } from "vitest";
import { useIframeStore } from "../../stores/iframeStore";
import * as api from "./api";
import { useWealthStore } from "./store";
import {
  canUseOperations,
  getOperationsBranches,
  getWorkbenchContext,
  useWealthOperations,
} from "./operations";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  fetchBootstrap: vi.fn(),
  fetchPlanList: vi.fn(),
  fetchScenesByCategory: vi.fn(),
  publishPlan: vi.fn(),
  removePlan: vi.fn(),
}));

const bootstrap = { plans: [], draft: { name: "", items: [] }, savedAt: "" };
const login = () =>
  useIframeStore.getState().setContext({
    userId: "80008888",
    bbk: "V00001",
    positionId: "RB0101",
    userName: "测试用户",
    orgCode: "test-org",
    orgLvl: "70",
    sysId: "retail",
  });
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

beforeEach(() => {
  vi.clearAllMocks();
  window.__env__ = { operationsUsersByBranch: { V00001: ["80008888"] } };
  useIframeStore.getState().clearContext();
  useWealthOperations.setState(useWealthOperations.getInitialState(), true);
  useWealthStore.setState(useWealthStore.getInitialState(), true);
  vi.mocked(api.fetchBootstrap).mockResolvedValue(bootstrap);
  login();
});

describe("运维身份与分行边界", () => {
  it.each([
    undefined,
    null,
    [],
    "V00001",
    123,
    true,
    {},
    { V00001: [] },
    { V00001: ["*"] },
    { V00001: "80008888" },
    { V00001: 80008888 },
    { V00001: [80008888] },
    { "110": ["80008888"] },
  ])("运行时配置缺失、错误或不匹配时拒绝开放：%j", (config) => {
    Object.assign(window, { __env__: { operationsUsersByBranch: config } });
    expect(canUseOperations()).toBe(false);
  });

  it("未加载运行时配置时不回退到代码白名单", () => {
    delete window.__env__;
    expect(canUseOperations()).toBe(false);
  });

  it("每次读取运行时配置，可增加用户且用户编号保留前导零", () => {
    window.__env__ = {
      operationsUsersByBranch: { V00001: ["000123", "80008889"] },
    };
    expect(canUseOperations()).toBe(false);
    useIframeStore.getState().setContext({ userId: "000123" });
    expect(canUseOperations()).toBe(true);
    useIframeStore.getState().setContext({ userId: "123" });
    expect(canUseOperations()).toBe(false);
    window.__env__ = { operationsUsersByBranch: { V00001: ["123"] } };
    expect(canUseOperations()).toBe(true);
  });

  it.each([
    ["V00001", "80008888", true],
    ["110", "80008888", false],
    ["V00001", "80008889", false],
    [null, "80008888", false],
    ["V00001", null, false],
  ])("同时匹配真实分行 %s 和用户 %s", (bbk, userId, allowed) => {
    useIframeStore.getState().setContext({ bbk, userId });
    expect(canUseOperations()).toBe(allowed);
  });

  it("分行列表动态读取并排除总行", () => {
    expect(getOperationsBranches()).toContainEqual({
      label: "北京分行",
      value: "110",
    });
    expect(getOperationsBranches().some((item) => item.value === "100")).toBe(
      false,
    );
  });

  it("默认关闭，未选分行不查询；选定后只覆盖工作台业务上下文", async () => {
    const original = useIframeStore.getState();
    expect(getWorkbenchContext()).toBe(original);
    await useWealthStore.getState().setOperationsMode(true);
    expect(api.fetchBootstrap).not.toHaveBeenCalled();
    expect(useWealthStore.getState().accountId).toBe("middle");
    expect(() => getWorkbenchContext()).toThrow("请选择运维分行");
    await useWealthStore.getState().setOperationsMode(true, "110");
    expect(getWorkbenchContext()).toMatchObject({
      bbk: "110",
      orgCode: "110",
      positionId: "RB0301",
      userId: "80008888",
      userName: "测试用户",
      sysId: "retail",
      orgLvl: "30",
    });
    expect(useIframeStore.getState()).toBe(original);
    await useWealthStore.getState().setOperationsMode(false);
    expect(getWorkbenchContext()).toBe(original);
    expect(useWealthStore.getState().accountId).toBe("rm");
  });

  it("拒绝总行、未知分行和非白名单，并在身份变化时立即停止覆盖身份", async () => {
    expect(await useWealthStore.getState().setOperationsMode(true, "100")).toBe(
      false,
    );
    expect(
      await useWealthStore.getState().setOperationsMode(true, "missing"),
    ).toBe(false);
    await useWealthStore.getState().setOperationsMode(true, "110");
    useIframeStore.getState().setContext({ userId: "someone-else" });
    expect(getWorkbenchContext()).toBe(useIframeStore.getState());
    expect(await useWealthStore.getState().setOperationsMode(true, "110")).toBe(
      false,
    );
  });
});

describe("运维切换隔离", () => {
  it("切换清空草稿、编辑态、场景、人员和任务数据，提交时禁止切换", async () => {
    await useWealthStore.getState().setOperationsMode(true, "110");
    useWealthStore.setState({
      draft: { name: "旧分行草稿", items: [] },
      editingId: "old-plan",
      targetSapIds: ["old-user"],
      targetsLoaded: true,
      savedAt: "12:00",
      scenesByCategory: { finance: [] },
      todayCustomerCount: 8,
    });
    useWealthStore.setState({ acting: true });
    expect(await useWealthStore.getState().setOperationsMode(false)).toBe(
      false,
    );
    useWealthStore.setState({ acting: false });
    await useWealthStore.getState().setOperationsMode(false);
    expect(useWealthStore.getState()).toMatchObject({
      draft: { name: "", items: [] },
      editingId: null,
      targetSapIds: [],
      targets: [],
      scenesByCategory: {},
      todayCustomerCount: 0,
      savedAt: "",
    });
  });

  it("旧初始化和场景响应不能回写到新分行", async () => {
    const init = deferred<typeof bootstrap>();
    const scenes =
      deferred<Awaited<ReturnType<typeof api.fetchScenesByCategory>>>();
    vi.mocked(api.fetchBootstrap).mockReturnValueOnce(init.promise);
    const oldInit = useWealthStore.getState().init();
    vi.mocked(api.fetchScenesByCategory).mockReturnValueOnce(scenes.promise);
    const oldScenes = useWealthStore.getState().loadScenes("finance");
    await useWealthStore.getState().setOperationsMode(true, "110");
    init.resolve({ ...bootstrap, draft: { name: "旧草稿", items: [] } });
    scenes.resolve([{ id: "old-scene" } as never]);
    await Promise.all([oldInit, oldScenes]);
    expect(useWealthStore.getState().draft.name).toBe("");
    expect(useWealthStore.getState().scenesByCategory).toEqual({});
  });

  it("旧规划刷新结果不能覆盖切换后的列表", async () => {
    const plans = deferred<Awaited<ReturnType<typeof api.fetchPlanList>>>();
    vi.mocked(api.fetchPlanList).mockReturnValueOnce(plans.promise);
    const old = useWealthStore.getState().refreshPlans();
    await useWealthStore.getState().setOperationsMode(true, "110");
    plans.resolve([{ id: "old-plan" } as never]);
    await old;
    expect(useWealthStore.getState().plans).toEqual([]);
  });

  it("运维模式只允许明确授权的规划编辑与删除", async () => {
    await useWealthStore.getState().setOperationsMode(true, "110");
    useWealthStore.setState({
      plans: [{ id: "other", name: "其他人的规划", editable: false } as never],
    });
    useWealthStore.getState().editPlan("other");
    await useWealthStore.getState().removePlan("other");
    expect(useWealthStore.getState().editingId).toBeNull();
    expect(api.removePlan).not.toHaveBeenCalled();
  });
});
