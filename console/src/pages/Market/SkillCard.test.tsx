import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MarketSkill } from "../../api/modules/market";
import { SkillCard } from "./SkillCard";

afterEach(cleanup);

const skill: MarketSkill = {
  item_id: "one",
  name: "异步统计技能",
  description: "卡片先显示",
  version: "1",
  creator_id: "u",
  creator_name: "U",
  category_id: null,
  bbk_ids: [],
  status: "active",
  created_at: null,
  updated_at: null,
  call_count: 12,
  user_count: 3,
};

describe("skill card independent statistics", () => {
  it("remains visible and clickable while statistics load, then fills counts", () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <SkillCard
        skill={skill}
        onClick={onClick}
        isManager={false}
        statsStatus="loading"
      />,
    );
    expect(screen.getByTitle("统计加载中")).toHaveAttribute(
      "aria-busy",
      "true",
    );
    expect(screen.getAllByText("—")).toHaveLength(2);
    fireEvent.click(screen.getByText("异步统计技能"));
    expect(onClick).toHaveBeenCalledOnce();
    rerender(
      <SkillCard
        skill={skill}
        onClick={onClick}
        isManager={false}
        statsStatus="ready"
      />,
    );
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("shows unavailable counts without hiding the card", () => {
    render(
      <SkillCard
        skill={skill}
        onClick={() => {}}
        isManager={false}
        statsStatus="error"
      />,
    );
    expect(screen.getByText("异步统计技能")).toBeInTheDocument();
    expect(screen.getByTitle("统计暂不可用，刷新可重试")).toBeInTheDocument();
    expect(screen.getAllByText("—")).toHaveLength(2);
    expect(screen.queryByText("12")).not.toBeInTheDocument();
  });
});
