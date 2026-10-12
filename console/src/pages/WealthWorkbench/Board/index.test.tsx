import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useWealthStore } from "../store";
import type { WealthRole } from "../permissions";
import { todayKey } from "../utils";
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
    useWealthStore.setState({ accountId: "rm", plans: [], plansLoaded: false });
  });

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
