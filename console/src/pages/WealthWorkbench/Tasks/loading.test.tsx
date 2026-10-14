import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Tasks from "./index";
import type { Customer, Plan } from "../types";
import type { CustomerCounts } from "../api";

const state = vi.hoisted(() => ({
  customers: [] as Customer[],
  customersLoading: false,
  todayCounts: {
    targetCusCount: 1,
    todayTouchCount: 0,
    sevenDayTouchCount: 0,
    unTouchedCount: 1,
  } as CustomerCounts | null,
  pendingCounts: {
    targetCusCount: 1,
    todayTouchCount: 0,
    sevenDayTouchCount: 0,
    unTouchedCount: 1,
  } as CustomerCounts | null,
  doneCounts: {
    targetCusCount: 1,
    todayTouchCount: 0,
    sevenDayTouchCount: 0,
    unTouchedCount: 1,
  } as CustomerCounts | null,
  pendingCustomers: [] as Customer[],
  pendingLoading: false,
  doneCustomers: [] as Customer[],
  doneLoading: false,
  taskListsRefreshing: false,
  plans: [] as Plan[],
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
  vi.clearAllMocks();
  state.todayCounts =
    state.pendingCounts =
    state.doneCounts =
      {
        targetCusCount: 1,
        todayTouchCount: 0,
        sevenDayTouchCount: 0,
        unTouchedCount: 1,
      };
  state.plansLoaded = true;
  state.plans = [];
  state.customers = [];
  state.pendingCustomers = [];
  state.doneCustomers = [];
  state.customersLoading = false;
  state.pendingLoading = false;
  state.doneLoading = false;
  state.taskListsRefreshing = false;
});

describe("今日任务加载", () => {
  it.each(["today", "pending", "done"] as const)(
    "%s 显示自身后端四项汇总，零值保留，缺失字段显示占位",
    (page) => {
      const counts = {
        targetCusCount: 99,
        todayTouchCount: 0,
        sevenDayTouchCount: 30,
        unTouchedCount: 75,
      };
      state.todayCounts = state.pendingCounts = state.doneCounts = null;
      if (page === "today") state.todayCounts = counts;
      if (page === "pending") state.pendingCounts = counts;
      if (page === "done") state.doneCounts = counts;
      const { rerender } = render(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText(/目标客户/).querySelector("b")).toHaveTextContent(
        "99",
      );
      expect(
        screen.getByText(/今日已触达/).querySelector("b"),
      ).toHaveTextContent("0");
      expect(
        screen.getByText(/七日内已触达/).querySelector("b"),
      ).toHaveTextContent("30");
      expect(
        screen.getByText(/^待处理/, { selector: "span" }).querySelector("b"),
      ).toHaveTextContent("75");
      state.todayCounts = state.pendingCounts = state.doneCounts = null;
      rerender(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText(/目标客户/).querySelector("b")).toHaveTextContent(
        "--",
      );
      expect(
        screen.getByText(/七日内已触达/).querySelector("b"),
      ).toHaveTextContent("--");
    },
  );

  it("空名单显示所选场景执行时间，切换场景与刷新保留对应技能", () => {
    state.plans = [
      {
        id: "plan",
        name: "规划",
        source: "我的关注",
        desc: "",
        customers: 0,
        tasks: 0,
        rate: 0,
        status: "已自动下发",
        publishStatus: "published",
        period: "自定义",
        start: "2020-01-01",
        end: "2030-12-31",
        items: ["a", "b"].map((id) => ({
          id,
          sceneName: `场景${id}`,
          categoryLabel: "理财",
          categoryCode: "finance",
          mcpRelations: [],
          direction: "",
          cycle: "自定义",
          start: "2020-01-01",
          end: "2030-12-31",
          schedule: { type: "daily", hour: id === "a" ? 9 : 15, minute: 0 },
        })),
      },
    ];
    const { rerender } = render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(state.refreshTodayCustomers).toHaveBeenCalledExactlyOnceWith(
      "business",
      "a",
    );
    expect(screen.getByText("执行时间：每日 09:00")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "我的关注场景b" }));
    expect(state.loadTodayCustomers).toHaveBeenLastCalledWith("business", "b");
    expect(screen.getByText("执行时间：每日 15:00")).toBeInTheDocument();
    expect(state.refreshTodayCustomers).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "刷新工作任务" }));
    expect(state.refreshTodayCustomers).toHaveBeenLastCalledWith(
      "business",
      "b",
    );
    state.customersLoading = true;
    rerender(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
    state.customersLoading = false;
    state.customers = [
      {
        id: "customer",
        custUid: "customer",
        skillId: "b",
        name: "已生成客户",
        label: "",
        reason: "",
        category: "理财",
        task: "场景b",
        done: false,
        channel: "",
        time: "",
        note: "",
      },
    ];
    rerender(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "筛选接触状态" }));
    fireEvent.click(screen.getByRole("button", { name: /^已接触/ }));
    expect(screen.queryByText("已生成客户")).not.toBeInTheDocument();
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
    state.customers = [];
    fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <Tasks page="pending" />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <Tasks page="done" />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
  });

  it.each(["today", "pending", "done"] as const)(
    "%s 标签页后台刷新保留旧名单和统计，不显示加载占位，返回后更新数据",
    (page) => {
      const customer: Customer = {
        id: "cached",
        custUid: "cached",
        skillId: "skill",
        name: "现有客户",
        label: "",
        reason: "",
        category: "",
        task: "",
        touched: 0,
        done: page === "done",
        channel: "",
        time: "",
        note: "",
      };
      state.customers = [customer];
      state.pendingCustomers = [customer];
      state.doneCustomers = [customer];
      const { rerender } = render(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      if (page === "today")
        fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
      state.taskListsRefreshing = true;
      state.customersLoading = true;
      state.pendingLoading = true;
      state.doneLoading = true;
      rerender(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText("现有客户")).toBeInTheDocument();
      expect(screen.getByText(/目标客户/).querySelector("b")).toHaveTextContent(
        "1",
      );
      expect(screen.getByText("共 1 条记录")).toBeInTheDocument();
      expect(screen.queryByText(/客户清单加载中/)).not.toBeInTheDocument();
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
      const updated = { ...customer, id: "updated", name: "最新客户" };
      state.customers = [updated];
      state.pendingCustomers = [updated];
      state.doneCustomers = [updated];
      state.taskListsRefreshing = false;
      state.customersLoading = false;
      state.pendingLoading = false;
      state.doneLoading = false;
      rerender(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText("最新客户")).toBeInTheDocument();
      expect(screen.queryByText("现有客户")).not.toBeInTheDocument();
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    },
  );

  it.each(["today", "pending", "done"] as const)(
    "%s 加载时不展示缓存的26条统计和名单，返回后展示24条真实数据",
    (page) => {
      const cached = Array.from(
        { length: 26 },
        (_, index): Customer => ({
          id: String(index),
          custUid: String(index),
          skillId: "skill",
          name: `缓存客户${index}`,
          label: "",
          reason: "",
          category: "",
          task: "",
          touched: 0,
          done: page === "done",
          channel: "",
          time: "",
          note: "",
        }),
      );
      state.customers = cached;
      state.pendingCustomers = cached;
      state.doneCustomers = cached;
      const { rerender } = render(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      if (page === "today")
        fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
      state.customersLoading = true;
      state.pendingLoading = true;
      state.doneLoading = true;
      rerender(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText(/目标客户/).querySelector("b")).toHaveTextContent(
        "--",
      );
      expect(
        screen.getByText(/待处理/, { selector: "span" }).querySelector("b"),
      ).toHaveTextContent("--");
      expect(screen.getByRole("status")).toHaveTextContent("客户清单加载中");
      expect(screen.queryByText("缓存客户0")).not.toBeInTheDocument();
      expect(screen.getByText("共 -- 条记录")).toBeInTheDocument();
      const loaded = cached
        .slice(0, 24)
        .map((customer, index) => ({ ...customer, name: `返回客户${index}` }));
      state.todayCounts =
        state.pendingCounts =
        state.doneCounts =
          {
            targetCusCount: 24,
            todayTouchCount: 5,
            sevenDayTouchCount: 12,
            unTouchedCount: 19,
          };
      state.customers = loaded;
      state.pendingCustomers = loaded;
      state.doneCustomers = loaded;
      state.customersLoading = false;
      state.pendingLoading = false;
      state.doneLoading = false;
      rerender(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText(/目标客户/).querySelector("b")).toHaveTextContent(
        "24",
      );
      expect(screen.getByText("共 24 条记录")).toBeInTheDocument();
      expect(screen.getByText("返回客户0")).toBeInTheDocument();
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    },
  );

  it("进入页先刷新规划；切到客户视角只查名单，切回经营视角再刷新规划", () => {
    render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(state.refreshTodayCustomers).toHaveBeenCalledExactlyOnceWith(
      "business",
    );
    expect(state.loadTodayCustomers).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
    expect(state.loadTodayCustomers).toHaveBeenCalledExactlyOnceWith(
      "customer",
    );
    expect(state.refreshTodayCustomers).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByRole("button", { name: "刷新工作任务" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "经营视角" }));
    expect(state.refreshTodayCustomers).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "刷新工作任务" }));
    expect(state.refreshTodayCustomers).toHaveBeenCalledTimes(3);
    expect(state.loadTodayCustomers).toHaveBeenCalledTimes(1);
  });

  it("从其他任务页返回时，即使保留客户视角也重新刷新规划", () => {
    const { rerender } = render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
    rerender(
      <MemoryRouter>
        <Tasks page="pending" />
      </MemoryRouter>,
    );
    expect(state.loadPendingCustomers).toHaveBeenCalledTimes(1);
    rerender(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(state.refreshTodayCustomers).toHaveBeenLastCalledWith("customer");
    expect(state.refreshTodayCustomers).toHaveBeenCalledTimes(2);
    expect(state.loadTodayCustomers).toHaveBeenCalledTimes(1);
  });

  it("等待初始规划加载完成后再刷新", () => {
    state.plansLoaded = false;
    const { rerender } = render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(state.refreshTodayCustomers).not.toHaveBeenCalled();
    state.plansLoaded = true;
    rerender(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(state.refreshTodayCustomers).toHaveBeenCalledExactlyOnceWith(
      "business",
    );
  });
});
