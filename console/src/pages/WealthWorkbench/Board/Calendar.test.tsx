import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
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
  it("日历事项显示当天命中的场景名而不是计划名", () => {
    render(
      <Calendar
        sources={["分行关注", "行长关注", "我的关注"]}
        visible={[PLAN]}
        dimension="week"
        anchor="2026-09-14"
        onShift={vi.fn()}
        onToday={vi.fn()}
        onDimension={vi.fn()}
        onShowDetails={vi.fn()}
      />,
    );

    expect(screen.getByText("高净值客户经营")).toBeTruthy();
    expect(screen.getByText("理财到期提醒")).toBeTruthy();
    expect(screen.queryAllByText("九月重点规划")).toHaveLength(0);
  });
});
