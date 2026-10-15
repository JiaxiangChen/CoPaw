/**
 * 智能财富工作台 —— 任务树推导（buildTaskTree / planItemScheduledOn）测试
 * 任务页骨架的数据源：从「已发布且当日有排程」的规划场景推导任务树。
 */
import { describe, expect, it } from "vitest";
import type { Plan, PlanItem } from "./types";
import {
  buildTaskTree,
  cycleRange,
  DEFAULT_SCHEDULE,
  filterSceneConflictPlans,
  findSceneConflicts,
  planItemScheduledOn,
} from "./utils";

function makeItem(patch: Partial<PlanItem> = {}): PlanItem {
  return {
    id: "skill-wealth-insurance-1",
    sceneName: "保障潜客经营",
    categoryLabel: "保险",
    categoryCode: "insurance",
    mcpRelations: [],
    direction: "d",
    cycle: "本月",
    start: "2026-09-01",
    end: "2026-09-30",
    schedule: { ...DEFAULT_SCHEDULE },
    ...patch,
  };
}

function makePlan(patch: Partial<Plan> = {}): Plan {
  return {
    id: "plan-1",
    name: "九月重点客户经营计划",
    source: "行长关注",
    desc: "",
    customers: 0,
    tasks: 0,
    rate: 0,
    status: "已自动下发",
    publishStatus: "published",
    period: "本月",
    start: "2026-09-01",
    end: "2026-09-30",
    items: [makeItem()],
    ...patch,
  };
}

describe("buildTaskTree", () => {
  // 2026-09-14 是周一
  const MONDAY = "2026-09-14";

  it("已发布规划的当日排程场景进入任务树并按大类分组", () => {
    const plan = makePlan({
      items: [
        makeItem(),
        makeItem({
          id: "skill-wealth-finance-3",
          sceneName: "产品到期承接",
          categoryLabel: "理财",
          categoryCode: "finance",
        }),
      ],
    });

    const tree = buildTaskTree([plan], MONDAY);

    expect(tree.map((g) => g.category)).toEqual(["保险", "理财"]);
    expect(tree[0].nodes[0]).toMatchObject({
      sceneId: "skill-wealth-insurance-1",
      sceneName: "保障潜客经营",
      planName: "九月重点客户经营计划",
      source: "行长关注",
    });
    expect(tree[0].nodes[0].schedule).toEqual(plan.items[0].schedule);
    expect(tree[1].nodes[0].sceneName).toBe("产品到期承接");
  });

  it("发布中与发布失败的规划不产生任务", () => {
    const publishing = makePlan({ id: "p1", publishStatus: "publishing" });
    const failed = makePlan({ id: "p2", publishStatus: "publish_failed" });

    expect(buildTaskTree([publishing, failed], MONDAY)).toEqual([]);
  });

  it("同一场景被多个规划引用时只出现一次", () => {
    const a = makePlan({ id: "p1", name: "规划A" });
    const b = makePlan({ id: "p2", name: "规划B", source: "分行关注" });

    const tree = buildTaskTree([a, b], MONDAY);

    expect(tree).toHaveLength(1);
    expect(tree[0].nodes).toHaveLength(1);
    expect(tree[0].nodes[0].planName).toBe("规划A");
  });

  it("weekly 排程仅命中对应星期", () => {
    const plan = makePlan({
      items: [
        makeItem({
          schedule: { type: "weekly", hour: 9, minute: 0, daysOfWeek: ["mon"] },
        }),
      ],
    });

    expect(buildTaskTree([plan], MONDAY)).toHaveLength(1);
    expect(buildTaskTree([plan], "2026-09-15")).toEqual([]); // 周二
  });

  it("规划或条目有效期外不产生任务", () => {
    const expiredPlan = makePlan({ start: "2026-08-01", end: "2026-08-31" });
    const expiredItem = makePlan({
      items: [makeItem({ start: "2026-09-01", end: "2026-09-10" })],
    });

    expect(buildTaskTree([expiredPlan], MONDAY)).toEqual([]);
    expect(buildTaskTree([expiredItem], MONDAY)).toEqual([]);
  });

  it("无场景条目的规划不产生任务节点", () => {
    const plan = makePlan({ items: [] });
    expect(buildTaskTree([plan], MONDAY)).toEqual([]);
  });
});

describe("planItemScheduledOn", () => {
  it("缺少条目有效期时视为无排程", () => {
    const plan = makePlan();
    expect(
      planItemScheduledOn(plan, makeItem({ start: undefined }), "2026-09-14"),
    ).toBe(false);
  });
});

describe("cycleRange（按传入当天动态计算）", () => {
  // 2026-09-14 是周一，Q3 = 07-01 ~ 09-30
  const TODAY = "2026-09-14";

  it("今日 / T+1日", () => {
    expect(cycleRange("今日", TODAY)).toEqual(["2026-09-14", "2026-09-14"]);
    expect(cycleRange("T+1日", TODAY)).toEqual(["2026-09-15", "2026-09-15"]);
  });

  it("本周为周一至周日", () => {
    expect(cycleRange("本周", TODAY)).toEqual(["2026-09-14", "2026-09-20"]);
  });

  it("本月 / 本季", () => {
    expect(cycleRange("本月", TODAY)).toEqual(["2026-09-01", "2026-09-30"]);
    expect(cycleRange("本季", TODAY)).toEqual(["2026-07-01", "2026-09-30"]);
  });

  it("跨年时本月/本季边界正确", () => {
    expect(cycleRange("本月", "2026-02-10")).toEqual([
      "2026-02-01",
      "2026-02-28",
    ]);
    expect(cycleRange("本季", "2026-12-31")).toEqual([
      "2026-10-01",
      "2026-12-31",
    ]);
    expect(cycleRange("T+1日", "2026-12-31")).toEqual([
      "2027-01-01",
      "2027-01-01",
    ]);
  });

  it("自定义与未知标签返回 null", () => {
    expect(cycleRange("自定义", TODAY)).toBeNull();
    expect(cycleRange("随便", TODAY)).toBeNull();
  });
});

describe("findSceneConflicts", () => {
  it.each([
    ["2026-10-01", "2026-10-31", false],
    ["2026-08-01", "2026-08-31", false],
    ["2026-09-30", "2026-10-31", true],
    ["2026-08-01", "2026-09-01", true],
    ["2026-09-10", "2026-09-20", true],
    ["2026-08-01", "2026-10-31", true],
    ["", "", true],
  ])("按任务区间判断 %s 至 %s 是否重叠", (start, end, conflict) => {
    const draft = { name: "新规划", items: [makeItem({ start, end })] };
    expect(findSceneConflicts([makePlan()], draft, null).length > 0).toBe(
      conflict,
    );
  });

  it("编辑原场景日期后重新检查其他规划，不漏掉较旧的重叠规划", () => {
    const september = makePlan({ id: "self" });
    const october = makePlan({
      id: "october",
      items: [makeItem({ start: "2026-10-01", end: "2026-10-31" })],
    });
    const november = makePlan({
      id: "november",
      items: [makeItem({ start: "2026-11-01", end: "2026-11-30" })],
    });
    const draft = { name: "编辑规划", items: october.items! };
    expect(
      findSceneConflicts([september, november, october], draft, "self"),
    ).toEqual([{ scene: draft.items[0], planName: october.name }]);
  });

  it("草稿场景被其他已发布/发布中规划占用时给出冲突与所在规划名", () => {
    const published = makePlan({ id: "p1", name: "九月规划" });
    const publishing = makePlan({
      id: "p2",
      name: "十月规划",
      publishStatus: "publishing",
      items: [makeItem({ id: "skill-wealth-finance-3" })],
    });
    const draft = {
      name: "新规划",
      items: [makeItem(), makeItem({ id: "skill-wealth-finance-3" })],
    };

    const conflicts = findSceneConflicts([published, publishing], draft, null);

    expect(conflicts).toEqual([
      { scene: draft.items[0], planName: "九月规划" },
      { scene: draft.items[1], planName: "十月规划" },
    ]);
  });

  it("发布失败的规划不产生占用；编辑模式排除自身", () => {
    const failed = makePlan({ id: "p1", publishStatus: "publish_failed" });
    const self = makePlan({ id: "p2" });
    const draft = { name: "x", items: [makeItem()] };

    expect(findSceneConflicts([failed], draft, null)).toEqual([]);
    expect(findSceneConflicts([self], draft, "p2")).toEqual([]);
  });

  it("编辑时仅检查新增场景，保留的历史场景不因其他规划占用而阻断", () => {
    const retained = makeItem({ id: "scene-retained" });
    const added = makeItem({ id: "scene-added" });
    const self = makePlan({ id: "self", items: [retained] });
    const other = makePlan({
      id: "other",
      name: "其他规划",
      items: [retained, added],
    });
    const draft = { name: "编辑规划", items: [retained, added] };

    expect(findSceneConflicts([self, other], draft, "self")).toEqual([
      { scene: added, planName: "其他规划" },
    ]);
  });

  it("场景均未被占用时无冲突", () => {
    const plan = makePlan({ items: [makeItem({ id: "skill-other" })] });
    const draft = { name: "x", items: [makeItem()] };

    expect(findSceneConflicts([plan], draft, null)).toEqual([]);
  });
});

describe("filterSceneConflictPlans", () => {
  const middle = makePlan({ id: "middle", source: "分行关注" });
  const president = makePlan({ id: "president", source: "行长关注" });
  const ownRm = makePlan({
    id: "own-rm",
    source: "我的关注",
    editable: true,
  });
  const otherRm = makePlan({
    id: "other-rm",
    source: "我的关注",
    editable: false,
  });

  it("按中台、行长、客户经理的逐级规则筛选占用规划", () => {
    const plans = [middle, president, ownRm, otherRm];

    expect(filterSceneConflictPlans(plans, "middle")).toEqual([middle]);
    expect(filterSceneConflictPlans(plans, "president")).toEqual([
      middle,
      president,
    ]);
    expect(filterSceneConflictPlans(plans, "rm")).toEqual([
      middle,
      president,
      ownRm,
    ]);
  });
});

it("中台给 A、B 发布后，C 的接收人员不冲突；人员重叠仍拦截", () => {
  const plan = makePlan({ source: "分行关注", targetSapIds: ["a", "b"] });
  const draft = { name: "C 支行规划", items: [makeItem()] };
  const plans = filterSceneConflictPlans([plan], "president");
  expect(findSceneConflicts(plans, draft, null, ["c"])).toEqual([]);
  expect(findSceneConflicts(plans, draft, null, [])).toEqual([]);
  expect(findSceneConflicts(plans, draft, null, ["a", "c"])).toHaveLength(1);
});
