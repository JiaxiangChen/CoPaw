import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Plan, PlanItem } from "../types";
import { Calendar } from "./Calendar";

function makeItem(id: string, sceneName: string, start: string): PlanItem {
  return {
    id,
    sceneName,
    categoryLabel: "理财",
    categoryCode: "finance",
    mcpRelations: [],
    direction: "客户经营",
    cycle: "自定义",
    start,
    end: start,
    schedule: { type: "daily", hour: 9, minute: 0 },
  };
}

const PLAN: Plan = {
  id: "plan-1",
  name: "九月重点规划",
  source: "我的关注",
  desc: "测试规划",
  customers: 10,
  tasks: 2,
  rate: 20,
  status: "已自动下发",
  period: "本月",
  start: "2026-09-01",
  end: "2026-09-30",
  items: [
    makeItem("scene-1", "高净值客户经营", "2026-09-14"),
    makeItem("scene-2", "理财到期提醒", "2026-09-15"),
  ],
};

describe("Calendar", () => {
  afterEach(cleanup);

  it.each(["week", "month"] as const)(
    "%s 日历标题与列表使用相同名称",
    (dimension) => {
      const onShowDetails = vi.fn();
      render(
        <Calendar
          sources={["分行关注", "行长关注", "我的关注"]}
          visible={[PLAN]}
          planStat={() => null}
          dimension={dimension}
          anchor="2026-09-14"
          onShift={vi.fn()}
          onToday={vi.fn()}
          onDimension={vi.fn()}
          onShowDetails={onShowDetails}
        />,
      );

      const events = screen.getAllByTitle(
        "九月重点规划 · 已自动下发 · 点击查看详情",
      );
      expect(events).toHaveLength(2);
      expect(screen.getAllByText(PLAN.name)).toHaveLength(2);
      expect(screen.queryByText("高净值客户经营")).toBeNull();
      expect(screen.queryByText("理财到期提醒")).toBeNull();
      fireEvent.click(events[0]);
      expect(onShowDetails).toHaveBeenCalledWith("2026-09-14", PLAN.id);
      fireEvent.click(events[1]);
      expect(onShowDetails).toHaveBeenCalledWith("2026-09-15", PLAN.id);
    },
  );
});
