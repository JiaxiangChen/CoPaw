import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfigProvider } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useIframeStore } from "../../stores/iframeStore";
import WealthWorkbench from "./index";
import * as api from "./api";
import {
  fetchDistributeBranches,
  fetchDistributeTargets,
} from "./distributeTargets";
import { getWorkbenchContext, useWealthOperations } from "./operations";
import { useWealthStore } from "./store";
import type { Plan, PlanItem, Scene } from "./types";

vi.mock("../../constants/bbk", async (original) => ({
  ...(await original<typeof import("../../constants/bbk")>()),
  BBK_ID_MAP: [
    { label: "总行", value: "100" },
    { label: "北京分行", value: "110" },
    { label: "测试B分行", value: "120" },
  ],
}));
vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  fetchBootstrap: vi.fn(),
  fetchPlanList: vi.fn(),
  fetchScenesByCategory: vi.fn(),
  publishPlan: vi.fn(),
}));
vi.mock("./distributeTargets", () => ({
  fetchDistributeBranches: vi.fn(),
  fetchDistributeTargets: vi.fn(),
}));
vi.mock("./Board", () => ({
  default: function BoardStub() {
    const plans = useWealthStore((state) => state.plans);
    return <div>规划看板内容：{plans.map((plan) => plan.name).join("、")}</div>;
  },
}));
vi.mock("./Tasks", () => ({ default: () => <div>客户经理任务</div> }));
vi.mock("./useVisibilityRefresh", () => ({ useVisibilityRefresh: vi.fn() }));
vi.mock("./components/DialogHost", () => ({
  DialogHost: () => {
    const dialog = useWealthStore((state) => state.dialog);
    return dialog ? (
      <div role="dialog">
        {dialog.body}
        {dialog.buttons.map((button) => (
          <button
            key={button.label}
            onClick={() => {
              if (button.onClick) button.onClick();
              else useWealthStore.getState().closeDialog();
            }}
          >
            {button.label}
          </button>
        ))}
      </div>
    ) : null;
  },
}));

const scene: Scene = {
  id: "scene",
  name: "测试经营场景",
  category: "理财",
  categoryCode: "finance",
  icon: "chart",
  desc: "经营方向",
  source: "分行自建",
  mcpRelations: [],
};
const item: PlanItem = {
  id: "scene",
  sceneName: "测试经营场景",
  categoryCode: "finance",
  categoryLabel: "理财",
  mcpRelations: [],
  direction: "经营方向",
  cycle: "自定义",
  start: "2026-10-01",
  end: "2026-10-31",
  schedule: { type: "daily", hour: 9, minute: 0 },
};
const makePlan = (id: string): Plan => ({
  id,
  name: id,
  source: "分行关注",
  desc: "",
  customers: 0,
  tasks: 0,
  rate: 0,
  status: "已自动下发",
  publishStatus: "published",
  editable: true,
  period: "自定义",
  start: "2026-10-01",
  end: "2026-10-31",
  items: [],
});
const bootstrap = (id: string): api.BootstrapData => ({
  plans: [makePlan(id)],
  draft: { name: "", items: [] },
  savedAt: "",
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.__env__ = { operationsUsersByBranch: { V00001: ["80008888"] } };
  useWealthOperations.setState(useWealthOperations.getInitialState(), true);
  useWealthStore.setState(useWealthStore.getInitialState(), true);
  useIframeStore.getState().clearContext();
  useIframeStore.getState().setContext({
    userId: "80008888",
    bbk: "V00001",
    positionId: "RB0101",
    userName: "测试用户",
    orgCode: "test-original-org",
    orgLvl: "70",
    sysId: "retail",
  });
  vi.mocked(api.fetchBootstrap).mockImplementation(async () =>
    bootstrap(`plan-${getWorkbenchContext().bbk}`),
  );
  vi.mocked(api.fetchPlanList).mockResolvedValue([]);
  vi.mocked(api.fetchScenesByCategory).mockResolvedValue([scene]);
  vi.mocked(fetchDistributeBranches).mockImplementation(async (branch) =>
    branch
      ? []
      : [
          {
            mOrgCode: `${getWorkbenchContext().bbk}001`,
            mOrgName: `${getWorkbenchContext().bbk}支行`,
            mOrgLvl: 80,
            leaf: true,
          },
        ],
  );
  vi.mocked(fetchDistributeTargets).mockImplementation(async () => [
    {
      sapId: `user-${getWorkbenchContext().bbk}`,
      name: `${getWorkbenchContext().bbk}客户经理`,
      orgName: "测试支行",
      bbkId: getWorkbenchContext().bbk!,
      bbkOrgId: `${getWorkbenchContext().bbk}001`,
    },
  ]);
});
afterEach(cleanup);

function mount() {
  return render(
    <ConfigProvider prefixCls="swe">
      <MemoryRouter initialEntries={["/wealth/board"]}>
        <Routes>
          <Route path="/wealth/*" element={<WealthWorkbench />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>,
  );
}
async function selectBranch(label: string) {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "运维分行" }));
  fireEvent.click(await screen.findByText(label, { exact: true }));
}

describe("运维开启与切换分行的完整状态边界", () => {
  it("真实创建页选择场景和人员后，取消切换保留原内容，确认切换清空并加载新分行", async () => {
    const originalIdentity = useIframeStore.getState();
    mount();
    fireEvent.click(await screen.findByRole("switch", { name: "运维模式" }));
    await selectBranch("北京分行");
    await screen.findByText("规划看板内容：plan-110");
    fireEvent.click(screen.getByRole("button", { name: "创建计划" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /测试经营场景/ }),
    );
    fireEvent.change(screen.getByLabelText("规划名称"), {
      target: { value: "A分行草稿" },
    });
    const branchSelect = screen.getByRole("combobox", { name: "选择支行" });
    await waitFor(() => expect(branchSelect).toBeEnabled());
    fireEvent.mouseDown(branchSelect);
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /110支行/ }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /110客户经理/ }));
    expect(useWealthStore.getState().targetSapIds).toEqual(["user-110"]);

    await selectBranch("测试B分行");
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "取消" }),
    );
    expect(getWorkbenchContext().bbk).toBe("110");
    expect(useWealthStore.getState().draft.name).toBe("A分行草稿");
    expect(useWealthStore.getState().targetSapIds).toEqual(["user-110"]);

    await selectBranch("测试B分行");
    fireEvent.click(screen.getByRole("button", { name: "清空并切换" }));
    await screen.findByText("规划看板内容：plan-120");
    expect(getWorkbenchContext()).toMatchObject({
      bbk: "120",
      orgCode: "120",
      positionId: "RB0301",
    });
    expect(useWealthStore.getState()).toMatchObject({
      accountId: "middle",
      draft: { name: "", items: [] },
      editingId: null,
      targets: [],
      targetSapIds: [],
      scenesByCategory: {},
    });
    expect(useIframeStore.getState()).toBe(originalIdentity);

    fireEvent.click(screen.getByRole("button", { name: "创建计划" }));
    const nextBranchSelect = screen.getByRole("combobox", { name: "选择支行" });
    await waitFor(() => expect(nextBranchSelect).toBeEnabled());
    fireEvent.mouseDown(nextBranchSelect);
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /120支行/ }),
    );
    await screen.findByRole("button", { name: /120客户经理/ });
    expect(
      screen.queryByRole("button", { name: /110客户经理/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("规划名称")).toHaveValue("");
    fireEvent.click(screen.getByRole("switch", { name: "运维模式" }));
    await screen.findByText("规划看板内容：plan-V00001");
    expect(useWealthStore.getState().accountId).toBe("rm");
    expect(getWorkbenchContext()).toBe(originalIdentity);
    expect(
      screen.getByRole("button", { name: "今日任务" }),
    ).toBeInTheDocument();
  }, 15_000);

  it("快速 A→B→A 时，旧 A 的初始化、刷新、场景和人员结果均不能覆盖新 A", async () => {
    const oldInit = deferred<api.BootstrapData>();
    const oldPlans = deferred<Plan[]>();
    const oldScenes = deferred<Scene[]>();
    const oldTargets =
      deferred<Awaited<ReturnType<typeof fetchDistributeTargets>>>();
    vi.mocked(api.fetchBootstrap).mockReturnValueOnce(oldInit.promise);
    const initializing = useWealthStore
      .getState()
      .setOperationsMode(true, "110");
    vi.mocked(api.fetchPlanList).mockReturnValueOnce(oldPlans.promise);
    vi.mocked(api.fetchScenesByCategory).mockReturnValueOnce(oldScenes.promise);
    vi.mocked(fetchDistributeTargets).mockReturnValueOnce(oldTargets.promise);
    const refreshing = useWealthStore.getState().refreshPlans();
    const loadingScenes = useWealthStore.getState().loadScenes("finance");
    const loadingTargets = useWealthStore.getState().loadTargets({
      mOrgCode: "110001",
      mOrgName: "A支行",
      mOrgLvl: 80,
      leaf: true,
    });
    await useWealthStore.getState().setOperationsMode(true, "120");
    expect(useWealthStore.getState().plans[0].id).toBe("plan-120");
    vi.mocked(api.fetchBootstrap).mockResolvedValueOnce(bootstrap("new-A"));
    await useWealthStore.getState().setOperationsMode(true, "110");
    oldInit.resolve(bootstrap("old-A"));
    oldPlans.resolve([makePlan("old-refresh")]);
    oldScenes.resolve([scene]);
    oldTargets.resolve([
      { sapId: "old-user", name: "旧人员", orgName: "A支行" },
    ]);
    await Promise.all([
      initializing,
      refreshing,
      loadingScenes,
      loadingTargets,
    ]);
    expect(getWorkbenchContext().bbk).toBe("110");
    expect(useWealthStore.getState().plans.map((plan) => plan.id)).toEqual([
      "new-A",
    ]);
    expect(useWealthStore.getState()).toMatchObject({
      targets: [],
      targetSapIds: [],
      scenesByCategory: {},
      scenesLoading: false,
      accountId: "middle",
    });
  });

  it("发布在途禁止手动切换，身份退出后的旧发布结果不能回写普通模式", async () => {
    await useWealthStore.getState().setOperationsMode(true, "110");
    const pendingPublish = deferred<api.PublishResult>();
    vi.mocked(api.publishPlan).mockReturnValueOnce(pendingPublish.promise);
    useWealthStore.setState({
      draft: { name: "A分行规划", items: [item] },
      targetSapIds: ["user-110"],
      targets: [
        {
          sapId: "user-110",
          name: "接收人",
          orgName: "A支行",
          bbkId: "110",
          bbkOrgId: "110001",
        },
      ],
    });
    const publishing = useWealthStore.getState().publishPlan();
    expect(useWealthStore.getState().acting).toBe(true);
    expect(await useWealthStore.getState().setOperationsMode(true, "120")).toBe(
      false,
    );
    expect(await useWealthStore.getState().setOperationsMode(false)).toBe(
      false,
    );
    expect(getWorkbenchContext().bbk).toBe("110");
    await useWealthStore.getState().setOperationsMode(false, null, true);
    await useWealthStore.getState().init();
    pendingPublish.resolve({
      plans: [makePlan("late-publish-A")],
      created: true,
      planId: "late-publish-A",
    });
    expect(await publishing).toBe(false);
    expect(useWealthStore.getState()).toMatchObject({
      accountId: "rm",
      acting: false,
      toastText: "",
    });
    expect(useWealthStore.getState().plans.map((plan) => plan.id)).toEqual([
      "plan-V00001",
    ]);
  });
});
