import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Tasks from "./index";
import type { Customer } from "../types";

const state = vi.hoisted(() => ({
  customers: [] as Customer[],
  customersLoading: false,
  pendingCustomers: [] as Customer[],
  pendingLoading: false,
  doneCustomers: [] as Customer[],
  doneLoading: false,
  plans: [],
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
  state.plansLoaded = true;
  state.customers = [];
  state.pendingCustomers = [];
  state.doneCustomers = [];
  state.customersLoading = false;
  state.pendingLoading = false;
  state.doneLoading = false;
});

describe("今日任务加载", () => {
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
