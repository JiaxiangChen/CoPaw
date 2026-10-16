import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useWealthStore } from "../store";
import { fetchSkillStats } from "../api";
import type { SkillStat } from "../types";
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
    vi.mocked(fetchSkillStats).mockResolvedValue({});
    useWealthStore.setState({ accountId: "rm", plans: [], plansLoaded: false });
  });

  it.each([
    ["publishing", "发布中", "发布中"],
    ["publish_failed", "发布失败", "发布失败"],
    ["published", "分发中", "发布成功"],
    ["published", "分发失败", "发布成功"],
    ["published", "已自动下发", "发布成功"],
  ])(
    "%s / %s：列表与详情只展示发布状态",
    async (publishStatus, status, label) => {
      const day = todayKey();
      useWealthStore.setState({
        plansLoaded: true,
        plans: [
          {
            id: "status-plan",
            name: "状态测试规划",
            source: "我的关注",
            desc: "测试",
            customers: 0,
            tasks: 0,
            rate: 0,
            publishStatus,
            status,
            period: "今日",
            start: day,
            end: day,
            items: [
              {
                id: "status-skill",
                sceneName: "测试场景",
                categoryLabel: "理财",
                categoryCode: "finance",
                mcpRelations: [],
                direction: "客户经营",
                cycle: "今日",
                start: day,
                end: day,
                schedule: { type: "daily", hour: 9, minute: 0 },
              },
            ],
          },
        ],
      });
      const view = render(
        <MemoryRouter>
          <Board />
        </MemoryRouter>,
      );
      fireEvent.click(
        screen.getByRole("button", {
          name: "状态测试规划，1 个技能，点击查看详情",
        }),
      );
      const detail = useWealthStore.getState().dialog!.body;
      fireEvent.click(screen.getByRole("button", { name: "列表模式" }));
      const list = within(screen.getByRole("article"));
      expect(list.getByText(label)).toBeInTheDocument();
      if (status !== label)
        expect(list.queryByText(status)).not.toBeInTheDocument();
      view.unmount();
      render(<>{detail}</>);
      expect(
        screen.getByRole("columnheader", { name: "发布状态" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("columnheader", { name: "分发结果" }),
      ).not.toBeInTheDocument();
      expect(screen.getByText(label)).toBeInTheDocument();
      if (status !== label)
        expect(screen.queryByText(status)).not.toBeInTheDocument();
      await act(async () => {});
    },
  );

  it("自定义日期按闭区间交集筛选，与来源筛选组合并保留上次选择", async () => {
    const month = todayKey().slice(0, 7);
    useWealthStore.setState({
      plansLoaded: true,
      plans: [
        ["边界重叠规划", "行长关注", "10", "11"],
        ["范围内规划", "我的关注", "12", "15"],
        ["范围前规划", "我的关注", "01", "03"],
        ["范围后规划", "我的关注", "21", "25"],
      ].map(([name, source, start, end], index) => ({
        id: `custom-${index}`,
        name,
        source,
        desc: "测试规划",
        customers: 0,
        tasks: 0,
        rate: 0,
        status: "已自动下发",
        period: index === 1 ? "本季" : "自定义",
        start: `${month}-${start}`,
        end: `${month}-${end}`,
        items: [],
      })),
    });
    render(
      <MemoryRouter>
        <Board />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "列表模式" }));
    fireEvent.click(screen.getByRole("button", { name: "自定义" }));
    fireEvent.click(screen.getByPlaceholderText("开始日期"));
    fireEvent.click(
      document.querySelector(`[title="${month}-11"] .ant-picker-cell-inner`)!,
    );
    fireEvent.click(
      document.querySelector(`[title="${month}-20"] .ant-picker-cell-inner`)!,
    );
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));
    expect(screen.getByText("边界重叠规划")).toBeInTheDocument();
    expect(screen.getByText("范围内规划")).toBeInTheDocument();
    expect(screen.queryByText("范围前规划")).not.toBeInTheDocument();
    expect(screen.queryByText("范围后规划")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "我的关注（1）" }));
    expect(screen.getAllByRole("article")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "全部（2）" }));
    fireEvent.click(screen.getByRole("button", { name: "本月" }));
    expect(screen.getAllByRole("article")).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: "自定义" }));
    expect(screen.getByPlaceholderText("开始日期")).toHaveValue(`${month}-11`);
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue(`${month}-20`);
    expect(screen.getAllByRole("article")).toHaveLength(2);
  });

  it.each(["success", "zero", "missing", "unavailable"] as const)(
    "%s 时日历与列表复用规划统计，加载中显示占位",
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
      const event = screen.getByRole("button", {
        name: "统计一致性规划，2 个技能，点击查看详情",
      });
      expect(event).toHaveTextContent("-- 位目标客户");
      expect(event).toHaveTextContent("已生成 -- 项任务");
      const stats =
        state === "unavailable"
          ? null
          : state === "missing"
          ? {}
          : {
              "stats-plan": {
                targetCustomerCount: state === "zero" ? 0 : 20,
                generatedTaskCount: state === "zero" ? 0 : 7,
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
      if (state === "unavailable" || state === "missing") {
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
