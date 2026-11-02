import { beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../../api/request";
import { useIframeStore } from "../../stores/iframeStore";
import * as api from "./api";
import {
  fetchDistributeBranches,
  fetchDistributeTargets,
} from "./distributeTargets";
import { operationsIdentity, useWealthOperations } from "./operations";
import type { Account } from "./types";

vi.mock("../../api/request", () => ({ request: vi.fn() }));

function activate(branchId: string | null = "110") {
  useWealthOperations.setState({
    enabled: true,
    branchId,
    identity: operationsIdentity(),
  });
}

const middle: Account = {
  id: "middle",
  name: "测试用户",
  role: "分行中台",
  department: "110",
  source: "分行关注",
};
const view = {
  id: "plan",
  name: "代建规划",
  publishStatus: "published",
  boardStatus: "已自动下发",
  scenes: [],
  targets: [],
  editable: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  window.__env__ = { operationsUsersByBranch: { V00001: ["80008888"] } };
  useWealthOperations.setState(useWealthOperations.getInitialState(), true);
  useIframeStore.getState().clearContext();
  useIframeStore.getState().setContext({
    userId: "80008888",
    userName: "测试用户",
    bbk: "V00001",
    positionId: "RB0101",
    orgCode: "test-org",
    orgLvl: "70",
    sysId: "retail",
    source: "RCZT",
  });
  vi.mocked(request).mockImplementation(async (path) => ({
    code: "200",
    data: String(path).includes("?")
      ? { items: [view] }
      : { id: "plan", publishStatus: "publishing" },
  }));
});

describe("运维模式现有接口适配", () => {
  it("列表按目标分行中台查询，身份和通用鉴权构造器保持原样", async () => {
    activate();
    const original = useIframeStore.getState();
    await api.fetchPlanList(true);
    const [path, options] = vi.mocked(request).mock.calls[0];
    const query = new URL(String(path), "http://localhost").searchParams;
    expect(Object.fromEntries(query)).toEqual({
      sapId: "80008888",
      role: "middle",
      bbkId: "110",
    });
    expect(options).toEqual({});
    expect(useIframeStore.getState()).toBe(original);
  });

  it.each([null, "plan"])(
    "创建/修改 %s 保留真实操作人、来源和接收人机构",
    async (editingId) => {
      activate();
      await api.publishPlan(
        middle,
        { name: "代建规划", items: [] },
        editingId,
        [
          {
            sapId: "0001",
            name: "客户经理",
            orgName: "支行",
            bbkId: "110",
            bbkOrgId: "110001",
          },
        ],
      );
      const [, options] = vi.mocked(request).mock.calls[0];
      expect(options?.method).toBe(editingId ? "PUT" : "POST");
      expect(JSON.parse(String(options?.body))).toMatchObject({
        sapId: "80008888",
        creatorName: "测试用户",
        role: "middle",
        bbkId: "110",
        bbkOrgId: "110",
        sourceId: "RCZT",
        sourceLabel: "分行关注",
        targets: [{ sapId: "0001", bbkId: "110", bbkOrgId: "110001" }],
      });
    },
  );

  it("仅在运维 DELETE 补目标分行，普通 DELETE 请求体不变", async () => {
    vi.mocked(request).mockResolvedValue({ code: "200", data: true });
    vi.mocked(request)
      .mockResolvedValueOnce({ code: "200", data: true })
      .mockResolvedValueOnce({ code: "200", data: { items: [] } });
    activate();
    await api.removePlan("plan");
    expect(
      JSON.parse(String(vi.mocked(request).mock.calls[0][1]?.body)),
    ).toEqual({ sapId: "80008888", role: "middle", bbkId: "110" });
    useWealthOperations.setState({ enabled: false, branchId: null });
    vi.mocked(request)
      .mockResolvedValueOnce({ code: "200", data: true })
      .mockResolvedValueOnce({ code: "200", data: { items: [] } });
    await api.removePlan("plan");
    expect(
      JSON.parse(String(vi.mocked(request).mock.calls[2][1]?.body)),
    ).toEqual({ sapId: "80008888" });
  });

  it("场景、机构树和人员跟随目标分行，下级机构继续使用服务返回的级别", async () => {
    activate();
    vi.mocked(request).mockResolvedValue({ code: "200", data: [] });
    await api.fetchScenesByCategory("finance");
    await fetchDistributeBranches();
    await fetchDistributeTargets({
      mOrgCode: "110001",
      mOrgName: "支行",
      mOrgLvl: 80,
      leaf: true,
    });
    const calls = vi.mocked(request).mock.calls;
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual({
      bbkId: "110",
      category: "finance",
    });
    expect(JSON.parse(String(calls[1][1]?.body))).toEqual({
      mmOrgCode: "110",
      mmOrgLvl: 70,
      sysId: "retail",
    });
    expect(JSON.parse(String(calls[2][1]?.body))).toEqual({
      bbkOrgId: "110",
      mmOrgCode: "110001",
      mmOrgLvl: 80,
    });
  });

  it("运维草稿不能覆盖普通中台草稿，运维初始化不恢复普通草稿", async () => {
    await api.saveDraft("middle", { name: "普通中台草稿", items: [] });
    activate();
    expect((await api.fetchBootstrap("middle")).draft.name).toBe("");
    await api.saveDraft("middle", { name: "运维草稿", items: [] });
    useWealthOperations.setState({ enabled: false, branchId: null });
    expect((await api.fetchBootstrap("middle")).draft.name).toBe(
      "普通中台草稿",
    );
  });

  it("运维规划缺少明确 editable 时映射为只读", async () => {
    activate();
    vi.mocked(request).mockResolvedValue({
      code: "200",
      data: { items: [{ ...view, editable: undefined }] },
    });
    expect((await api.fetchPlanList(true))[0].editable).toBe(false);
  });
});
