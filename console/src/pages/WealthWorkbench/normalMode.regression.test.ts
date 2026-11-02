import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../../api/request";
import { useAgentStore } from "../../stores/agentStore";
import { useIframeStore } from "../../stores/iframeStore";
import * as api from "./api";
import {
  fetchDistributeBranches,
  fetchDistributeTargets,
} from "./distributeTargets";
import { getWorkbenchContext, useWealthOperations } from "./operations";
import { ROLE_ACCOUNT_META, type WealthRole } from "./permissions";
import { useWealthStore } from "./store";
import type { Account } from "./types";

vi.mock("../../api/request", () => ({ request: vi.fn() }));

const roles = [
  ["RB0101", "rm"],
  ["RB1101", "president"],
  ["RB0306", "president"],
  ["RB0108", "president"],
  ["RB0301", "middle"],
  ["RB0305", "middle"],
] as const;

const plan = {
  id: "normal-plan",
  name: "原有规划",
  sourceLabel: "我的关注",
  scenes: [],
  targets: [],
  publishStatus: "published",
  boardStatus: "已自动下发",
};

beforeEach(() => {
  vi.clearAllMocks();
  useWealthOperations.setState(useWealthOperations.getInitialState(), true);
  useWealthStore.setState(useWealthStore.getInitialState(), true);
  useIframeStore.getState().clearContext();
  useIframeStore.getState().setContext({
    userId: "00001234",
    bbk: "110",
    orgCode: "110007",
    positionId: "RB0101",
    userName: "原登录姓名",
    orgLvl: "70",
    sysId: "retail",
    source: "RCZT",
  });
  useAgentStore.getState().setSelectedAgent("default");
  vi.mocked(request).mockImplementation(async (path, options) => {
    if (String(path).includes("/plans?"))
      return { code: "200", data: { items: [plan] } };
    if (options?.method === "DELETE") return { code: "200", data: true };
    if (String(path).includes("/plans"))
      return { code: "200", data: { id: plan.id, publishStatus: "published" } };
    return { code: "200", data: [] };
  });
});
afterEach(() => useAgentStore.getState().setSelectedAgent("default"));

describe("运维关闭时保留原有业务契约", () => {
  it.each(roles)(
    "岗位 %s 沿用原角色 %s，初始化不切换身份或上下文",
    async (positionId, role) => {
      useIframeStore.getState().setContext({ positionId });
      const original = useIframeStore.getState();
      const bootstrap = vi.spyOn(api, "fetchBootstrap");
      await useWealthStore.getState().init();
      expect(useWealthStore.getState().accountId).toBe(role);
      expect(bootstrap).toHaveBeenCalledTimes(1);
      expect(bootstrap).toHaveBeenCalledWith(role);
      expect(useWealthStore.getState().contextVersion ?? 0).toBe(0);
      expect(getWorkbenchContext()).toBe(original);
      expect(useIframeStore.getState()).toBe(original);
      expect(useWealthOperations.getState().enabled).toBe(false);
      bootstrap.mockRestore();
    },
  );

  it.each(roles)(
    "岗位 %s 的规划读写完整参数保持原契约",
    async (positionId, role) => {
      useIframeStore.getState().setContext({ positionId });
      const account: Account = {
        id: role,
        name: "原登录姓名",
        department: "110007",
        ...ROLE_ACCOUNT_META[role],
      };
      await api.fetchPlanList(true);
      expect(vi.mocked(request).mock.calls[0][0]).toBe(
        `/zt/wealth/plans?sapId=00001234&role=${role}&bbkId=110`,
      );
      for (const editingId of [null, plan.id]) {
        vi.mocked(request).mockClear();
        await api.publishPlan(
          account,
          { name: "普通规划", items: [] },
          editingId,
          [
            {
              sapId: "000001",
              name: "接收人",
              orgName: "原支行",
              bbkId: "110",
              bbkOrgId: "110008",
            },
          ],
        );
        expect(vi.mocked(request).mock.calls[0]).toEqual([
          editingId ? `/zt/wealth/plans/${plan.id}` : "/zt/wealth/plans",
          {
            method: editingId ? "PUT" : "POST",
            body: JSON.stringify({
              sapId: "00001234",
              role,
              bbkId: "110",
              bbkOrgId: "110007",
              creatorName: "原登录姓名",
              sourceId: "RCZT",
              agentId: "default",
              name: "普通规划",
              sourceLabel: ROLE_ACCOUNT_META[role].source,
              scenes: [],
              targets: [
                {
                  sapId: "000001",
                  name: "接收人",
                  bbkId: "110",
                  bbkOrgId: "110008",
                },
              ],
            }),
          },
        ]);
      }
      vi.mocked(request).mockClear();
      await api.removePlan(plan.id);
      expect(vi.mocked(request).mock.calls[0]).toEqual([
        `/zt/wealth/plans/${plan.id}`,
        { method: "DELETE", body: JSON.stringify({ sapId: "00001234" }) },
      ]);
    },
  );

  it("白名单用户只要没有开启，仍以真实岗位和测试分行正常请求", async () => {
    useIframeStore.getState().setContext({
      userId: "80008888",
      bbk: "V00001",
      orgCode: "test-original-org",
      positionId: "RB1101",
    });
    // 即使有残留候选值，也不能在开关关闭时覆盖真实业务身份。
    useWealthOperations.setState({
      enabled: false,
      branchId: "110",
      identity: "old-context",
    });
    await api.fetchPlanList(true);
    expect(vi.mocked(request).mock.calls[0][0]).toBe(
      "/zt/wealth/plans?sapId=80008888&role=president&bbkId=V00001",
    );
    expect(getWorkbenchContext()).toBe(useIframeStore.getState());
  });

  it("普通模式场景、机构根及下级人员仍取原登录机构参数", async () => {
    await api.fetchScenesByCategory("finance");
    await fetchDistributeBranches();
    await fetchDistributeTargets();
    await fetchDistributeTargets({
      mOrgCode: "110009",
      mOrgName: "下级支行",
      mOrgLvl: 80,
      leaf: true,
    });
    const bodies = vi
      .mocked(request)
      .mock.calls.map(([, options]) => JSON.parse(String(options?.body)));
    expect(bodies).toEqual([
      { bbkId: "110", category: "finance" },
      { mmOrgCode: "110", mmOrgLvl: 70, sysId: "retail" },
      { bbkOrgId: "110", mmOrgCode: "110007", mmOrgLvl: 70 },
      { bbkOrgId: "110", mmOrgCode: "110009", mmOrgLvl: 80 },
    ]);
  });

  it.each([true, false, undefined])(
    "普通模式保留原 editable=%s 的映射，不套用运维只读策略",
    async (editable) => {
      vi.mocked(request).mockResolvedValue({
        code: "200",
        data: { items: [{ ...plan, editable }] },
      });
      expect((await api.fetchPlanList(true))[0].editable).toBe(editable);
    },
  );

  it.each([null, "UNKNOWN_POSITION"])(
    "原未识别岗位 %s 继续拦截，不加载业务数据",
    async (positionId) => {
      useIframeStore.getState().setContext({ positionId });
      await useWealthStore.getState().init();
      expect(useWealthStore.getState().accountId).toBe("unknown" as WealthRole);
      expect(request).not.toHaveBeenCalled();
    },
  );
});
