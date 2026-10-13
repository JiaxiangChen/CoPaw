import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useWealthStore } from "../store";
import { fetchSkillStats } from "../api";
import type { SkillStat } from "../types";
import type { WealthRole } from "../permissions";
import { sceneStatKey, todayKey } from "../utils";
import Board from "./index";

vi.mock("../api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api")>();
  return {
    ...original,
    fetchAvailableSceneCount: vi.fn().mockResolvedValue(0),
    fetchSkillStats: vi.fn().mockResolvedValue({}),
  };
});

describe("Board", () => {
  afterEach(cleanup);
  beforeEach(() => {
    vi.mocked(fetchSkillStats).mockResolvedValue({});
    useWealthStore.setState({ accountId: "rm", plans: [], plansLoaded: false });
  });

  it.each(["success", "zero", "unavailable"] as const)(
    "%s 时日历与列表复用技能统计，加载中显示占位",
    async (state) => {
      const day = todayKey();
      let resolveStats!: (value: Record<string, SkillStat> | null) => void;
      vi.mocked(fetchSkillStats).mockReturnValue(
        new Promise((resolve) => {
          resolveStats = resolve;
        }),
      );
      useWealthStore.setState({
        plansLoaded: true,
        plans: [
          {
            id: "stats-plan",
            name: "统计一致性规划",
            source: "我的关注",
            desc: "测试规划",
            customers: 999,
            tasks: 888,
            rate: 0,
            status: "已自动下发",
            period: "今日",
            start: day,
            end: day,
            items: ["skill-1", "skill-2"].map((id) => ({
              id,
              sceneName: id,
              categoryLabel: "理财",
              categoryCode: "finance",
              mcpRelations: [],
              direction: "客户经营",
              cycle: "今日",
              start: day,
              end: day,
              schedule: { type: "daily", hour: 9, minute: 0 },
            })),
          },
        ],
      });
      render(
        <MemoryRouter>
          <Board />
        </MemoryRouter>,
      );
      const event = screen.getByTitle(
        "skill-1 / skill-2 · 已自动下发 · 点击查看详情",
      );
      expect(event).toHaveTextContent("-- 位目标客户");
      expect(event).toHaveTextContent("已生成 -- 项任务");
      const stats =
        state === "unavailable"
          ? null
          : state === "zero"
          ? {}
          : {
              [sceneStatKey({
                skillId: "skill-1",
                startDate: day,
                endDate: day,
              })]: {
                targetCustomerCount: 12,
                generatedTaskCount: 3,
              },
              [sceneStatKey({
                skillId: "skill-2",
                startDate: day,
                endDate: day,
              })]: {
                targetCustomerCount: 8,
                generatedTaskCount: 4,
              },
            };
      await act(async () => resolveStats(stats));
      const customers =
        state === "success" ? "20" : state === "zero" ? "0" : "--";
      const tasks = state === "success" ? "7" : state === "zero" ? "0" : "--";
      expect(event).toHaveTextContent(`${customers} 位目标客户`);
      expect(event).toHaveTextContent(`已生成 ${tasks} 项任务`);
      fireEvent.click(screen.getByRole("button", { name: "列表模式" }));
      const row = within(screen.getByRole("article"));
      if (state === "unavailable") {
        expect(row.getAllByText("--", { selector: "strong" })).toHaveLength(2);
      } else {
        expect(row.getByText(`${customers} 人`)).toBeInTheDocument();
        expect(row.getByText(`${tasks} 个`)).toBeInTheDocument();
      }
    },
  );

  it("规划加载中展示稳定占位，加载完成后才展示真实空态", () => {
    render(
      <MemoryRouter>
        <Board />
      </MemoryRouter>,
    );

    expect(screen.getByRole("status")).toHaveTextContent("正在加载规划数据");
    expect(screen.queryByText("当前筛选条件下暂无规划")).toBeNull();

    act(() => useWealthStore.setState({ plansLoaded: true }));
    fireEvent.click(screen.getByRole("button", { name: "列表模式" }));

    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("当前筛选条件下暂无规划")).toBeInTheDocument();
  });

  it.each<[WealthRole, readonly string[]]>([
    ["rm", ["分行关注", "行长关注", "我的关注"]],
    ["president", ["分行关注", "行长关注"]],
    ["middle", ["分行关注"]],
  ])("%s 的分类、全部数量及列表只包含可见来源", (role, sources) => {
    const allSources = ["分行关注", "行长关注", "我的关注"];
    useWealthStore.setState({
      accountId: role,
      plansLoaded: true,
      plans: allSources.map((source, index) => ({
        id: `plan-${index}`,
        name: `${source}规划`,
        source,
        desc: "测试规划",
        customers: 0,
        tasks: 0,
        rate: 0,
        status: "已自动下发",
        period: "本周",
        start: todayKey(),
        end: todayKey(),
        items: [],
      })),
    });
    render(
      <MemoryRouter>
        <Board />
      </MemoryRouter>,
    );
    expect(
      screen.getByRole("button", { name: `全部（${sources.length}）` }),
    ).toBeInTheDocument();
    for (const source of allSources) {
      expect(screen.queryByRole("button", { name: `${source}（1）` })).toEqual(
        sources.includes(source) ? expect.anything() : null,
      );
    }
    expect(screen.queryByText("行长 / 我的关注")).toEqual(
      role === "rm" ? expect.anything() : null,
    );
    expect(screen.queryByText("行长关注")).toEqual(
      role === "president" ? expect.anything() : null,
    );
    fireEvent.click(screen.getByRole("button", { name: "列表模式" }));
    for (const source of allSources) {
      expect(screen.queryByText(`${source}规划`)).toEqual(
        sources.includes(source) ? expect.anything() : null,
      );
    }
  });
});
