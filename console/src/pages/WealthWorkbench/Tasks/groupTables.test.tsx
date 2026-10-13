import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Customer } from "../types";
import Tasks from "./index";

const state = vi.hoisted(() => ({
  customers: [] as Customer[],
  customersLoading: false,
  pendingCustomers: [] as Customer[],
  pendingLoading: false,
  doneCustomers: [] as Customer[],
  doneLoading: false,
  plans: [
    {
      id: "plan",
      name: "经营规划",
      source: "我的关注",
      publishStatus: "published",
      start: "2020-01-01",
      end: "2030-12-31",
      items: [
        {
          id: "skill",
          sceneName: "资产到期经营",
          categoryLabel: "理财",
          start: "2020-01-01",
          end: "2030-12-31",
          schedule: { type: "daily" },
        },
      ],
    },
  ],
  plansLoaded: true,
  refreshTodayCustomers: vi.fn().mockResolvedValue(undefined),
  loadTodayCustomers: vi.fn().mockResolvedValue(undefined),
  loadPendingCustomers: vi.fn().mockResolvedValue(undefined),
  loadDoneCustomers: vi.fn().mockResolvedValue(undefined),
  openDialog: vi.fn(),
}));

vi.mock("../store", () => ({
  useCanAccess: () => true,
  useWealthStore: (selector: (value: typeof state) => unknown) =>
    selector(state),
}));

afterEach(cleanup);
beforeEach(() => {
  state.pendingCustomers = [];
  state.doneCustomers = [];
  state.customers = ["48", "48", "50"].map((age, index) => ({
    id: `c${index}`,
    custUid: `c${index}`,
    skillId: "skill",
    name: `客户${index}`,
    label: "",
    reason: "经营机会",
    category: "理财",
    task: "资产到期经营",
    done: false,
    channel: "",
    time: "",
    note: "",
    groupFields: [
      { name: "age", label: "年龄", value: age },
      { name: "risk", label: "风险等级", value: "稳健型" },
    ],
  }));
});

describe("经营分类独立表格", () => {
  it.each(["today", "pending", "done"] as const)(
    "%s 的经营场景默认两项，支持展开收起与查看完整名称",
    async (page) => {
      const fullName = "高净值客户理财到期资金承接与长期资产配置".repeat(4);
      state.customers[0].skillNames = [
        fullName,
        "信贷需求挖掘",
        "家庭保险保障",
        "存款资金承接",
      ];
      state.pendingCustomers = state.customers;
      state.doneCustomers = state.customers;
      render(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      if (page === "today")
        fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
      const preview = screen.getByText(fullName);
      const cell = preview.closest("td")!;
      expect(within(cell).getAllByRole("listitem")).toHaveLength(2);
      expect(within(cell).queryByText("家庭保险保障")).not.toBeInTheDocument();
      const toggle = within(cell).getByRole("button", {
        name: "展开剩余 2 项",
      });
      expect(toggle).toHaveAttribute("aria-expanded", "false");
      fireEvent.click(toggle);
      expect(within(cell).getAllByRole("listitem")).toHaveLength(4);
      expect(within(cell).getByText("家庭保险保障")).toBeInTheDocument();
      expect(toggle).toHaveAttribute("aria-expanded", "true");
      fireEvent.click(within(cell).getByRole("button", { name: "收起" }));
      expect(within(cell).getAllByRole("listitem")).toHaveLength(2);
      expect(preview).toHaveAttribute("tabindex", "0");
      fireEvent.mouseEnter(preview);
      expect(await screen.findByRole("tooltip")).toHaveTextContent(fullName);
      fireEvent.mouseLeave(preview);
      fireEvent.focus(preview);
      expect(await screen.findByRole("tooltip")).toHaveTextContent(fullName);
    },
  );

  it("今日任务客户视角在标签之后展示接口技能名称，缺失时保留占位", () => {
    state.customers[0].skillNames = ["接口技能一", "接口技能二"];
    const { rerender } = render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(
      screen.queryByRole("columnheader", { name: "经营场景" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
    const table = screen.getByRole("table");
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((th) => th.textContent),
    ).toEqual([
      "客户姓名",
      "重点标签",
      "经营场景",
      "经营机会",
      "客户指标",
      "经营方案",
      "执行",
    ]);
    const rows = within(table).getAllByRole("row");
    expect(within(rows[1]).getAllByRole("cell")[2]).toHaveTextContent(
      /接口技能一.*接口技能二/,
    );
    expect(within(rows[2]).getAllByRole("cell")[2]).toHaveTextContent("--");
    state.customers = [];
    rerender(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(
      screen.getByText("暂无符合条件的客户").closest("td"),
    ).toHaveAttribute("colspan", "7");
  });

  it.each(["pending", "done"] as const)(
    "%s 在标签之后展示接口技能名称，并保持空态列数一致",
    (page) => {
      const customers = state.customers.map((customer, index) => ({
        ...customer,
        skillNames: index === 0 ? ["接口技能一", "接口技能二"] : [],
        done: page === "done",
      }));
      state.pendingCustomers = customers;
      state.doneCustomers = customers;
      const { rerender } = render(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      const table = screen.getByRole("table");
      const headers = within(table).getAllByRole("columnheader");
      expect(headers.slice(0, 4).map((th) => th.textContent)).toEqual([
        "客户姓名",
        "重点标签",
        "经营场景",
        page === "done" ? "经营任务" : "经营机会",
      ]);
      expect(headers).toHaveLength(page === "done" ? 8 : 7);
      const rows = within(table).getAllByRole("row");
      expect(within(rows[1]).getAllByRole("cell")[2]).toHaveTextContent(
        /接口技能一.*接口技能二/,
      );
      expect(within(rows[2]).getAllByRole("cell")[2]).toHaveTextContent("--");
      state.pendingCustomers = [];
      state.doneCustomers = [];
      rerender(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByRole("cell")).toHaveAttribute(
        "colspan",
        page === "done" ? "8" : "7",
      );
    },
  );

  it("长技能名称的提示同时保留完整名称和来源规划，兼容键盘聚焦", async () => {
    const scene = state.plans[0].items[0];
    const originalName = scene.sceneName;
    const fullName = "高价值动账客户经营".repeat(8);
    scene.sceneName = fullName;
    try {
      render(
        <MemoryRouter>
          <Tasks page="today" />
        </MemoryRouter>,
      );
      const button = screen.getByRole("button", { name: new RegExp(fullName) });
      expect(button).not.toHaveAttribute("title");
      fireEvent.mouseEnter(button);
      const tooltip = await screen.findByRole("tooltip");
      expect(tooltip).toHaveTextContent(`技能名称：${fullName}`);
      expect(tooltip).toHaveTextContent("来源规划：经营规划");
      fireEvent.mouseLeave(button);
      fireEvent.focus(button);
      expect(await screen.findByRole("tooltip")).toHaveTextContent(fullName);
    } finally {
      scene.sceneName = originalName;
    }
  });

  it("每个分类有独立表头，分类栏位于表格外并显示人数", () => {
    render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    const tables = screen.getAllByRole("table");
    expect(tables).toHaveLength(2);
    expect(
      within(tables[0]).getByRole("columnheader", { name: "客户姓名" }),
    ).toBeInTheDocument();
    expect(
      within(tables[1]).getByRole("columnheader", { name: "客户姓名" }),
    ).toBeInTheDocument();
    const heading = screen.getByRole("button", {
      name: "年龄：48 / 风险等级：稳健型，2 人",
    });
    expect(heading).toHaveTextContent("年龄：48 / 风险等级：稳健型");
    expect(heading.closest("table")).toBeNull();
    expect(within(tables[0]).getByText("客户0")).toBeInTheDocument();
    expect(within(tables[0]).queryByText("客户2")).not.toBeInTheDocument();
    expect(within(tables[1]).getByText("客户2")).toBeInTheDocument();
    fireEvent.click(heading);
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(heading).toHaveAttribute("aria-expanded", "false");
    expect(
      document.getElementById(heading.getAttribute("aria-controls")!),
    ).toHaveAttribute("hidden");
    fireEvent.click(heading);
    expect(screen.getAllByRole("table")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(
      screen.queryByRole("button", {
        name: "年龄：48 / 风险等级：稳健型，2 人",
      }),
    ).not.toBeInTheDocument();
  });

  it("空分类配置保持一张表格，空名单也保留表头与空态", () => {
    state.customers = state.customers.map((customer) => ({
      ...customer,
      groupFields: [],
    }));
    const { rerender } = render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(
      screen.queryByRole("heading", { level: 3, name: /年龄/ }),
    ).not.toBeInTheDocument();
    state.customers = [];
    rerender(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(
      screen.getByRole("columnheader", { name: "客户姓名" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/暂无客户名单/)).toBeInTheDocument();
  });
});
