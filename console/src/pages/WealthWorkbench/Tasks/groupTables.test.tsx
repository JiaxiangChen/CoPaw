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
import styles from "../index.module.less";

const state = vi.hoisted(() => ({
  customers: [] as Customer[],
  customersLoading: false,
  todayCounts: {
    targetCusCount: 42,
    todayTouchCount: 5,
    sevenDayTouchCount: 10,
    unTouchedCount: 37,
  },
  pendingCounts: {
    targetCusCount: 42,
    todayTouchCount: 5,
    sevenDayTouchCount: 10,
    unTouchedCount: 37,
  },
  doneCounts: {
    targetCusCount: 42,
    todayTouchCount: 5,
    sevenDayTouchCount: 10,
    unTouchedCount: 37,
  },
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
  it("今日任务各分类内待处理优先，同状态保持原顺序，不改变分类和缓存顺序", () => {
    const template = state.customers[0];
    state.customers = [
      { name: "A1", age: "48", touched: 1 },
      { name: "B1", age: "50", touched: 1 },
      { name: "A2", age: "48", touched: 0 },
      { name: "A3", age: "48", touched: 3 },
      { name: "B2", age: "50", touched: 0 },
      { name: "A4", age: "48", touched: 0 },
    ].map(({ name, age, touched }) => ({
      ...template,
      id: name,
      name,
      touched,
      groupFields: [{ name: "age", label: "年龄", value: age }],
    }));
    render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    const names = (table: HTMLElement) =>
      within(table)
        .getAllByRole("row")
        .slice(1)
        .map((row) => within(row).getAllByRole("cell")[0].textContent);
    expect(screen.getAllByRole("table").map(names)).toEqual([
      ["A2", "A3", "A4", "A1"],
      ["B2", "B1"],
    ]);
    fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
    expect(names(screen.getByRole("table"))).toEqual([
      "A2",
      "A3",
      "B2",
      "A4",
      "A1",
      "B1",
    ]);
    expect(state.customers.map((customer) => customer.name)).toEqual([
      "A1",
      "B1",
      "A2",
      "A3",
      "B2",
      "A4",
    ]);
  });

  it("已完成列表与记录弹窗将强接触展示为已接触，其他方式保持原值", () => {
    state.doneCustomers = state.customers.map((customer, index) => ({
      ...customer,
      done: true,
      channel: ["强接触", "电话", "企微"][index],
    }));
    render(
      <MemoryRouter>
        <Tasks page="done" />
      </MemoryRouter>,
    );
    const cells = screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell")[4]);
    expect(cells[0]).toHaveTextContent("已接触");
    expect(cells[0]).not.toHaveTextContent("强接触");
    expect(cells[1]).toHaveTextContent("电话");
    expect(cells[2]).toHaveTextContent("企微");
    fireEvent.click(
      within(cells[0].closest("tr")!).getByRole("button", { name: "查看记录" }),
    );
    const calls = state.openDialog.mock.calls;
    const dialog = calls[calls.length - 1]?.[0];
    expect(dialog.title).toBe("客户触达记录");
    const { container } = render(dialog.body);
    expect(
      within(container).getByText("触达方式").parentElement,
    ).toHaveTextContent("已接触");
    expect(container).not.toHaveTextContent("强接触");
    expect(state.doneCustomers[0].channel).toBe("强接触");
  });

  it("已完成展示后端四项统计，搜索不改变统计口径", () => {
    state.doneCustomers = state.customers.map((customer) => ({
      ...customer,
      touched: 1,
      done: true,
    }));
    state.customers = [];
    render(
      <MemoryRouter>
        <Tasks page="done" />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/累计已完成|今日已完成/)).not.toBeInTheDocument();
    expect(screen.getByText(/七日内已触达/)).toHaveTextContent(
      "七日内已触达 10 人",
    );
    expect(screen.getByText(/目标客户/)).toHaveTextContent("目标客户 42 人");
    expect(screen.getByText(/今日已触达/)).toHaveTextContent("今日已触达 5 人");
    expect(screen.getByText(/^待处理/)).toHaveTextContent("待处理 37 人");
    fireEvent.change(
      screen.getByRole("textbox", { name: "搜索客户或经营机会" }),
      { target: { value: "客户1" } },
    );
    expect(screen.getByText(/七日内已触达/)).toHaveTextContent(
      "七日内已触达 10 人",
    );
    expect(screen.getByText(/目标客户/)).toHaveTextContent("目标客户 42 人");
    expect(screen.getByText(/今日已触达/)).toHaveTextContent("今日已触达 5 人");
    expect(screen.getByText(/^待处理/)).toHaveTextContent("待处理 37 人");
  });

  it.each(["business", "customer"] as const)(
    "%s 接触状态筛选联动名单且保留后端统计，支持清除和未知状态",
    (view) => {
      const template = state.customers[0];
      state.customers = [0, 1, 3, null].map((touched, index) => ({
        ...template,
        id: `status-${index}`,
        name: `状态客户${index}`,
        touched,
        label: index % 2 === 0 ? "行长指派" : "我的关注",
        groupFields: [],
      }));
      render(
        <MemoryRouter>
          <Tasks page="today" />
        </MemoryRouter>,
      );
      if (view === "customer")
        fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
      fireEvent.click(screen.getByRole("button", { name: "筛选接触状态" }));
      const dialog = screen.getByRole("dialog", { name: "筛选接触状态" });
      expect(
        within(dialog).getByRole("button", { name: /^全部状态/ }),
      ).toHaveTextContent("4");
      expect(
        within(dialog).getByRole("button", { name: /^--/ }),
      ).toHaveTextContent("2");
      fireEvent.click(within(dialog).getByRole("button", { name: /^已接触/ }));
      expect(screen.getAllByRole("row")).toHaveLength(2);
      expect(screen.getByText(/七日内已触达/)).toHaveTextContent(
        "七日内已触达 10 人",
      );
      expect(screen.getByText(/目标客户/)).toHaveTextContent("目标客户 42 人");
      expect(screen.getByText(/今日已触达/)).toHaveTextContent(
        "今日已触达 5 人",
      );
      expect(screen.getByText(/^待处理/)).toHaveTextContent("待处理 37 人");
      fireEvent.click(screen.getByRole("button", { name: "清除接触状态筛选" }));
      expect(screen.getAllByRole("row")).toHaveLength(5);
      fireEvent.click(screen.getByRole("button", { name: "筛选接触状态" }));
      fireEvent.click(
        within(screen.getByRole("dialog", { name: "筛选接触状态" })).getByRole(
          "button",
          { name: /^--/ },
        ),
      );
      expect(screen.getAllByRole("row")).toHaveLength(3);
      expect(screen.getByText(/^待处理/)).toHaveTextContent("待处理 37 人");
      if (view === "customer") {
        fireEvent.click(screen.getByRole("button", { name: "筛选重点标签" }));
        fireEvent.click(
          within(
            screen.getByRole("dialog", { name: "筛选重点标签" }),
          ).getByRole("button", { name: /^行长指派/ }),
        );
        expect(screen.getAllByRole("row")).toHaveLength(2);
        expect(screen.getByText("状态客户2")).toBeInTheDocument();
        fireEvent.click(
          screen.getByRole("button", { name: /筛选接触状态，当前/ }),
        );
        const choices = screen.getByRole("dialog", { name: "筛选接触状态" });
        expect(
          within(choices).getByRole("button", { name: /^全部状态/ }),
        ).toHaveTextContent("2");
        expect(
          within(choices).getByRole("button", { name: /^已接触/ }),
        ).toHaveTextContent("0");
      }
    },
  );

  it("接触状态筛选无结果时展示筛选空态，切换视角仍保留筛选", () => {
    state.customers = state.customers.map((customer) => ({
      ...customer,
      touched: 0,
      groupFields: [],
    }));
    render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "筛选接触状态" }));
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "筛选接触状态" })).getByRole(
        "button",
        { name: /^已接触/ },
      ),
    );
    expect(screen.getByText("暂无符合条件的客户")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
    expect(
      screen.getByRole("button", { name: "筛选接触状态，当前：已接触" }),
    ).toBeInTheDocument();
    expect(screen.getByText("暂无符合条件的客户")).toBeInTheDocument();
  });

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
      "接触状态",
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
    ).toHaveAttribute("colspan", "8");
  });

  it.each(["business", "customer"] as const)(
    "今日任务 %s 按 touched 展示接触状态，人数使用后端统计",
    (view) => {
      const statuses = [0, 1, 3, 2, -1, null, undefined];
      const template = state.customers[0];
      state.customers = statuses.map((touched, index) => ({
        ...template,
        id: `cust-${index}`,
        touched,
        // 故意与 touched 相反，验证接触状态展示以接口原值为准。
        done: touched !== 1,
        groupFields: [],
      }));
      const { rerender } = render(
        <MemoryRouter>
          <Tasks page="today" />
        </MemoryRouter>,
      );
      if (view === "customer") {
        fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
      }
      const headers = screen.getAllByRole("columnheader");
      const statusColumn = headers.findIndex(
        (th) => th.textContent === "接触状态",
      );
      expect(statusColumn).toBeGreaterThan(-1);
      const cells = screen
        .getAllByRole("row")
        .slice(1)
        .map((row) => within(row).getAllByRole("cell")[statusColumn]);
      expect(cells.map((cell) => cell.textContent)).toEqual([
        "未接触",
        "--",
        "--",
        "--",
        "--",
        "--",
        "已接触",
      ]);
      cells.forEach((cell, index) => {
        expect(cell.firstElementChild).toHaveClass(
          index === 6 ? styles.green : styles.emptyTag,
        );
      });
      expect(screen.getByText(/七日内已触达/)).toHaveTextContent(
        "七日内已触达 10 人",
      );
      expect(screen.getByText(/目标客户/)).toHaveTextContent("目标客户 42 人");
      expect(screen.getByText(/今日已触达/)).toHaveTextContent(
        "今日已触达 5 人",
      );
      expect(screen.getByText(/^待处理/)).toHaveTextContent("待处理 37 人");
      state.customers = [];
      rerender(
        <MemoryRouter>
          <Tasks page="today" />
        </MemoryRouter>,
      );
      expect(screen.getByRole("cell")).toHaveAttribute(
        "colspan",
        String(headers.length),
      );
    },
  );

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
        page === "done" ? "客户指标" : "经营机会",
      ]);
      expect(headers).toHaveLength(7);
      if (page === "done") {
        expect(headers.map((th) => th.textContent)).toEqual([
          "客户姓名",
          "重点标签",
          "经营场景",
          "客户指标",
          "触达方式",
          "完成时间",
          "经营结果",
        ]);
        expect(
          within(table).queryByRole("columnheader", { name: "经营任务" }),
        ).not.toBeInTheDocument();
      }
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
      expect(screen.getByRole("cell")).toHaveAttribute("colspan", "7");
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
