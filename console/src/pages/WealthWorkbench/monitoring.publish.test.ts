import { beforeEach, expect, it, vi } from "vitest";
import * as api from "./api";
import { useWealthStore } from "./store";
import { useIframeStore } from "../../stores/iframeStore";
import { defaultMonitoring } from "./monitoring";
import { cycleRange } from "./utils";

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  publishPlan: vi.fn(),
}));
beforeEach(() => {
  vi.resetAllMocks();
  useIframeStore.setState({ userId: "alice", source: "test", bbk: "bank" });
  useWealthStore.getState().resetPlanForm();
  const [start, end] = cycleRange("本月")!;
  useWealthStore.setState({
    accountId: "rm",
    acting: false,
    draft: {
      name: "监控规划",
      items: [
        {
          id: "scene-1",
          sceneName: "经营场景",
          categoryLabel: "基金",
          categoryCode: "fund",
          mcpRelations: [],
          direction: "经营方向",
          cycle: "本月",
          start,
          end,
          schedule: { type: "daily", hour: 9, minute: 0 },
        },
      ],
    },
  });
});

it("行长督办策略随草稿提交，不读写浏览器缓存", async () => {
  useWealthStore.setState({
    accountId: "president",
    targetSapIds: ["rm"],
    targets: [{ sapId: "rm", name: "客户经理", orgName: "支行" }],
  });
  const config = { ...defaultMonitoring(), expectedCompletionRate: 80 };
  useWealthStore.getState().setItemMonitoring("scene-1", config);
  const read = vi.spyOn(Storage.prototype, "getItem");
  const write = vi.spyOn(Storage.prototype, "setItem");
  vi.mocked(api.publishPlan).mockResolvedValue({
    plans: [],
    created: true,
    planId: "new-plan",
  });
  expect(await useWealthStore.getState().publishPlan()).toBe(true);
  expect(
    vi.mocked(api.publishPlan).mock.calls[0][1].items[0].monitoring,
  ).toEqual(config);
  expect(read).not.toHaveBeenCalled();
  expect(write).not.toHaveBeenCalled();
  read.mockRestore();
  write.mockRestore();
});
it("编辑使用接口规划中的督办策略，取消编辑不修改已保存规划", () => {
  const item = useWealthStore.getState().draft.items[0];
  const monitoring = { ...defaultMonitoring(), expectedCompletionRate: 85 };
  useWealthStore.setState({
    accountId: "president",
    plans: [
      {
        id: "saved",
        name: "已有规划",
        source: "行长关注",
        desc: "",
        customers: 0,
        tasks: 0,
        rate: 0,
        status: "已自动下发",
        period: "本月",
        start: item.start!,
        end: item.end!,
        items: [{ ...item, monitoring }],
      },
    ],
  });
  useWealthStore.getState().editPlan("saved");
  expect(useWealthStore.getState().draft.items[0].monitoring).toEqual(
    monitoring,
  );
  useWealthStore
    .getState()
    .setItemMonitoring("scene-1", { expectedCompletionRate: 90 });
  useWealthStore.getState().resetPlanForm();
  expect(
    useWealthStore.getState().plans[0].items![0].monitoring
      ?.expectedCompletionRate,
  ).toBe(85);
});
it("提交失败保留当前督办策略供重试", async () => {
  useWealthStore.setState({ accountId: "president", targetSapIds: ["rm"] });
  useWealthStore
    .getState()
    .setItemMonitoring("scene-1", { expectedCompletionRate: 80 });
  vi.mocked(api.publishPlan).mockRejectedValue(new Error("后台失败"));
  expect(await useWealthStore.getState().publishPlan()).toBe(false);
  expect(
    useWealthStore.getState().draft.items[0].monitoring?.expectedCompletionRate,
  ).toBe(80);
});
it("非行长不能设置督办策略", () => {
  useWealthStore
    .getState()
    .setItemMonitoring("scene-1", { expectedCompletionRate: 80 });
  expect(useWealthStore.getState().draft.items[0].monitoring).toBeUndefined();
});

it("发布给多位接收人时保留各自机构，不使用创建人机构替代", async () => {
  useIframeStore.setState({ bbk: "creator-bank", orgCode: "creator-org" });
  const targets = [
    {
      sapId: "rm-a",
      name: "甲",
      orgName: "甲支行",
      bbkId: "00100",
      bbkOrgId: "001001",
    },
    {
      sapId: "rm-b",
      name: "乙",
      orgName: "乙支行",
      bbkId: "00200",
      bbkOrgId: "002002",
    },
  ];
  useWealthStore.setState({
    accountId: "president",
    targets,
    targetSapIds: targets.map((t) => t.sapId),
  });
  vi.mocked(api.publishPlan).mockResolvedValue({
    plans: [],
    created: true,
    planId: "saved",
  });
  expect(await useWealthStore.getState().publishPlan()).toBe(true);
  expect(vi.mocked(api.publishPlan).mock.calls[0][3]).toEqual(targets);
});
it("客户经理发给本人时使用本人的分行和机构", async () => {
  useIframeStore.setState({
    userId: "rm-self",
    bbk: "self-bank",
    orgCode: "self-org",
  });
  vi.mocked(api.publishPlan).mockResolvedValue({
    plans: [],
    created: true,
    planId: "saved",
  });
  expect(await useWealthStore.getState().publishPlan()).toBe(true);
  expect(vi.mocked(api.publishPlan).mock.calls[0][3][0]).toMatchObject({
    sapId: "rm-self",
    bbkId: "self-bank",
    bbkOrgId: "self-org",
  });
});
