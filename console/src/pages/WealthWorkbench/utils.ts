/**
 * 智能财富工作台 —— 日期与排程工具
 * 排程模型与控制台定时任务一致（@/utils/parseCron 的 CronParts）。
 */
import type { CronParts } from "@/utils/parseCron";
import type { WealthRole } from "./permissions";
import type { Draft, Plan, PlanItem } from "./types";

/** 规划展示状态只反映发布生命周期，不展示后端聚合的分发结果。 */
export function planPublishLabel(plan: Plan): string {
  switch (plan.publishStatus) {
    case "publishing":
      return "发布中";
    case "published":
      return "发布成功";
    case "publish_failed":
      return "发布失败";
    default:
      return "状态未知";
  }
}

export function calendarDate(value: string): Date {
  return new Date(value + "T12:00:00");
}

export function dateKey(date: Date): string {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}

/** 当天日期键（替代原 mock 的硬编码 TODAY） */
export function todayKey(): string {
  return dateKey(new Date());
}

/** 任务周期快捷选项（含「自定义」；校验场景周期值用。本周仅看板列表筛选用，不在此列） */
export const CYCLE_LABELS = ["本月", "本季", "今日", "T+1日", "自定义"];

/**
 * 周期快捷选项 → 起止日期（按当天动态计算，替代原 mock 的硬编码区间）。
 * 未识别/自定义返回 null，由调用方决定保留原值还是给默认。
 */
export function cycleRange(
  label: string,
  today: string = todayKey(),
): [string, string] | null {
  const d = calendarDate(today);
  const year = d.getFullYear();
  const month = d.getMonth();
  switch (label) {
    case "今日":
      return [today, today];
    case "T+1日": {
      const next = addCalendarDays(today, 1);
      return [next, next];
    }
    case "本周": {
      const start = addCalendarDays(today, -((d.getDay() + 6) % 7));
      return [start, addCalendarDays(start, 6)];
    }
    case "本月":
      return [
        dateKey(new Date(year, month, 1, 12)),
        dateKey(new Date(year, month + 1, 0, 12)),
      ];
    case "本季": {
      const quarterStart = Math.floor(month / 3) * 3;
      return [
        dateKey(new Date(year, quarterStart, 1, 12)),
        dateKey(new Date(year, quarterStart + 3, 0, 12)),
      ];
    }
    default:
      return null;
  }
}

export function addCalendarDays(value: string, n: number): string {
  const date = calendarDate(value);
  date.setDate(date.getDate() + n);
  return dateKey(date);
}

/** 日历模式下的可见区间：月维度为整月，周维度为周一至周日 */
export function calendarRange(
  anchor: string,
  dimension: "week" | "month",
): [string, string] {
  const date = calendarDate(anchor);
  if (dimension === "month") {
    return [
      dateKey(new Date(date.getFullYear(), date.getMonth(), 1, 12)),
      dateKey(new Date(date.getFullYear(), date.getMonth() + 1, 0, 12)),
    ];
  }
  const start = addCalendarDays(anchor, -((date.getDay() + 6) % 7));
  return [start, addCalendarDays(start, 6)];
}

export function validTaskDate(value: string | undefined): boolean {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = calendarDate(value);
  if (Number.isNaN(date.getTime())) return false;
  return dateKey(date) === value;
}

const WEEKDAY_LABELS: Record<string, string> = {
  mon: "一",
  tue: "二",
  wed: "三",
  thu: "四",
  fri: "五",
  sat: "六",
  sun: "日",
};

const WEEKDAY_BY_INDEX = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

/** 默认排程：每日 09:00（与控制台定时任务表单的缺省一致） */
export const DEFAULT_SCHEDULE: CronParts = {
  type: "daily",
  hour: 9,
  minute: 0,
};

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/** 排程的人类可读摘要：每日 09:00 / 每周一、四 08:30 / 每小时整点 / 自定义表达式 */
export function scheduleLabel(parts: CronParts | undefined): string {
  const schedule = parts ?? DEFAULT_SCHEDULE;
  const time = `${pad2(schedule.hour ?? 9)}:${pad2(schedule.minute ?? 0)}`;
  switch (schedule.type) {
    case "hourly":
      return "每小时整点";
    case "daily":
      return `每日 ${time}`;
    case "weekly": {
      const days = (schedule.daysOfWeek?.length ? schedule.daysOfWeek : ["mon"])
        .map((d) => WEEKDAY_LABELS[d] ?? d)
        .join("、");
      return `每周${days} ${time}`;
    }
    case "custom":
      return schedule.rawCron || "自定义排程";
    default:
      return `每日 ${time}`;
  }
}

/** 五字段 cron 表达式的最小校验（自定义排程用） */
export function validCronExpr(value: string | undefined): boolean {
  if (!value) return false;
  return /^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$/.test(value.trim());
}

/** 规划的执行排程展示：各场景排程摘要去重拼接 */
export function planFrequency(p: Plan): string {
  const set = new Set((p.items ?? []).map((x) => scheduleLabel(x.schedule)));
  return [...set].join(" / ") || scheduleLabel(undefined);
}

/** 单个场景条目在某天是否有排程：需在规划与条目有效期内，weekly 需命中星期 */
export function planItemScheduledOn(
  p: Plan,
  item: PlanItem,
  day: string,
): boolean {
  if (day < p.start || day > p.end) return false;
  if (!item.start || !item.end) return false;
  if (day < item.start || day > item.end) return false;
  const schedule = item.schedule ?? DEFAULT_SCHEDULE;
  if (schedule.type !== "weekly") return true;
  const dayName = WEEKDAY_BY_INDEX[calendarDate(day).getDay()];
  const days = schedule.daysOfWeek?.length ? schedule.daysOfWeek : ["mon"];
  return days.includes(dayName);
}

/** 规划在某天是否有排程：按各场景的 CronParts 推算，自定义表达式周期内均显示 */
export function planScheduledOn(p: Plan, day: string): boolean {
  if (day < p.start || day > p.end) return false;
  if (!p.items?.length) return true;
  return p.items.some((x) => planItemScheduledOn(p, x, day));
}

/** 今日任务树节点：一个排程到当天的场景条目 */
export interface TaskTreeNode {
  schedule: CronParts;
  sceneId: string;
  sceneName: string;
  /** 来源规划名（hover 提示用） */
  planName: string;
  /** 来源标签：分行关注 / 行长关注 / 我的关注 */
  source: string;
}

/** 今日任务树分组：按产品大类聚合 */
export interface TaskTreeGroup {
  category: string;
  nodes: TaskTreeNode[];
}

/**
 * 今日任务树：从「已发布且今日有排程」的规划场景推导。
 * 未发布成功（publishing/publish_failed）的规划不产生任务；
 * 同一场景被多个规划引用时只出现一次，来源取第一个命中的规划。
 */
export function buildTaskTree(plans: Plan[], day: string): TaskTreeGroup[] {
  const groups: TaskTreeGroup[] = [];
  const seen = new Set<string>();
  for (const p of plans) {
    if (p.publishStatus !== "published") continue;
    for (const item of p.items ?? []) {
      if (seen.has(item.id) || !planItemScheduledOn(p, item, day)) continue;
      seen.add(item.id);
      const category = item.categoryLabel || "其他";
      let group = groups.find((g) => g.category === category);
      if (!group) {
        group = { category, nodes: [] };
        groups.push(group);
      }
      group.nodes.push({
        schedule: item.schedule,
        sceneId: item.id,
        sceneName: item.sceneName,
        planName: p.name,
        source: p.source,
      });
    }
  }
  return groups;
}

/** 发布确认前的场景冲突项：草稿场景已被哪个规划占用 */
export interface SceneConflict {
  scene: PlanItem;
  planName: string;
}

const RESERVING_SOURCES_BY_ROLE: Record<WealthRole, readonly string[]> = {
  middle: ["分行关注"],
  president: ["分行关注", "行长关注"],
  rm: ["分行关注", "行长关注"],
  unknown: [],
};

/** 按发布角色筛选会占用经营场景的规划。客户经理另受本人规划约束。 */
export function filterSceneConflictPlans(
  plans: Plan[],
  role: WealthRole,
): Plan[] {
  const sources = RESERVING_SOURCES_BY_ROLE[role];
  return plans.filter(
    (plan) =>
      sources.includes(plan.source) ||
      (role === "rm" && plan.editable === true),
  );
}

/**
 * 发布前置冲突检查：草稿中的场景若已被其他
 * 已发布/发布中的规划占用且任务区间重叠，则不允许重复发布。
 * 发布失败不占用；编辑时排除自身及日期未变的原场景。
 */
export function findSceneConflicts(
  plans: Plan[],
  draft: Draft,
  editingId: string | null,
  targetSapIds?: readonly string[],
): SceneConflict[] {
  const retainedItems =
    plans.find((plan) => plan.id === editingId)?.items ?? [];
  return draft.items
    .filter(
      (item) =>
        !retainedItems.some(
          (retained) =>
            retained.id === item.id &&
            retained.start === item.start &&
            retained.end === item.end,
        ),
    )
    .flatMap((scene) => {
      const owner = plans.find(
        (plan) =>
          plan.id !== editingId &&
          plan.publishStatus !== "publish_failed" &&
          (targetSapIds === undefined ||
            (plan.targetSapIds ?? []).some((id) =>
              targetSapIds.includes(id),
            )) &&
          (plan.items ?? []).some(
            (item) =>
              item.id === scene.id &&
              !(scene.start && item.end && scene.start > item.end) &&
              !(scene.end && item.start && scene.end < item.start),
          ),
      );
      return owner ? [{ scene, planName: owner.name }] : [];
    });
}
