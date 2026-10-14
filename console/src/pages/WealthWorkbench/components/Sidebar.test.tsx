import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";

const mocks = vi.hoisted(() => ({
  loadTodayCustomerCount: vi.fn(),
  loadPendingCustomers: vi.fn(),
  loadDoneCustomers: vi.fn(),
  state: {
    todayCustomerCount: 3,
    plansLoaded: true,
    pendingCustomers: [{ id: "pending-1" }, { id: "pending-2" }],
    doneCustomers: [{ id: "done-1" }],
  },
}));

vi.mock("../store", () => ({
  useCanAccess: () => true,
  useWealthStore: (selector: (state: object) => unknown) =>
    selector({
      ...mocks.state,
      loadTodayCustomerCount: mocks.loadTodayCustomerCount,
      loadPendingCustomers: mocks.loadPendingCustomers,
      loadDoneCustomers: mocks.loadDoneCustomers,
    }),
}));

describe("Sidebar", () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
    mocks.state.plansLoaded = true;
    mocks.state.todayCustomerCount = 3;
  });

  it("按客户视角总人数展示角标，保留待触达和已完成数量角标", async () => {
    render(
      <MemoryRouter initialEntries={["/wealth/board"]}>
        <Sidebar collapsed={false} onToggleCollapse={vi.fn()} />
      </MemoryRouter>,
    );

    expect(
      within(screen.getByRole("button", { name: /今日任务/ })).getByText("3"),
    ).toBeTruthy();
    expect(
      within(screen.getByRole("button", { name: /待触达客户/ })).getByText("2"),
    ).toBeTruthy();
    expect(
      within(screen.getByRole("button", { name: /已完成/ })).getByText("1"),
    ).toBeTruthy();

    await waitFor(() => {
      expect(mocks.loadTodayCustomerCount).toHaveBeenCalledOnce();
      expect(mocks.loadPendingCustomers).toHaveBeenCalledOnce();
      expect(mocks.loadDoneCustomers).toHaveBeenCalledOnce();
    });
  });

  it("客户总数更新后刷新角标，无客户时隐藏角标", () => {
    const sidebar = () => (
      <MemoryRouter initialEntries={["/wealth/board"]}>
        <Sidebar collapsed={false} onToggleCollapse={vi.fn()} />
      </MemoryRouter>
    );
    const { rerender } = render(sidebar());

    mocks.state.todayCustomerCount = 1;
    rerender(sidebar());
    expect(
      within(screen.getByRole("button", { name: /今日任务/ })).getByText("1"),
    ).toBeTruthy();

    mocks.state.todayCustomerCount = 0;
    rerender(sidebar());
    expect(screen.getByRole("button", { name: /今日任务/ }).textContent).toBe(
      "今日任务",
    );
  });

  it("规划数据就绪前不查询名单，就绪后补发请求", async () => {
    mocks.state.plansLoaded = false;

    const { rerender } = render(
      <MemoryRouter initialEntries={["/wealth/board"]}>
        <Sidebar collapsed={false} onToggleCollapse={vi.fn()} />
      </MemoryRouter>,
    );

    expect(mocks.loadTodayCustomerCount).not.toHaveBeenCalled();
    expect(mocks.loadPendingCustomers).not.toHaveBeenCalled();
    expect(mocks.loadDoneCustomers).not.toHaveBeenCalled();

    mocks.state.plansLoaded = true;
    rerender(
      <MemoryRouter initialEntries={["/wealth/board"]}>
        <Sidebar collapsed={false} onToggleCollapse={vi.fn()} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mocks.loadTodayCustomerCount).toHaveBeenCalledOnce();
      expect(mocks.loadPendingCustomers).toHaveBeenCalledOnce();
      expect(mocks.loadDoneCustomers).toHaveBeenCalledOnce();
    });
  });
});
