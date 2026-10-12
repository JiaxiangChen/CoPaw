import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";

const mocks = vi.hoisted(() => ({
  loadPendingCustomers: vi.fn(),
  loadDoneCustomers: vi.fn(),
  state: {
    plans: [],
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
      loadPendingCustomers: mocks.loadPendingCustomers,
      loadDoneCustomers: mocks.loadDoneCustomers,
    }),
}));

vi.mock("../utils", () => ({
  buildTaskTree: () => [
    { category: "理财", nodes: [{ sceneId: "scene-1" }] },
    {
      category: "存款",
      nodes: [{ sceneId: "scene-2" }, { sceneId: "scene-3" }],
    },
  ],
  todayKey: () => "2026-09-18",
}));

describe("Sidebar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.state.plansLoaded = true;
  });

  it("隐藏今日任务角标，保留待触达和已完成数量角标", async () => {
    render(
      <MemoryRouter initialEntries={["/wealth/board"]}>
        <Sidebar collapsed={false} onToggleCollapse={vi.fn()} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("button", { name: /今日任务/ }).textContent).toBe(
      "今日任务",
    );
    expect(
      within(screen.getByRole("button", { name: /待触达客户/ })).getByText("2"),
    ).toBeTruthy();
    expect(
      within(screen.getByRole("button", { name: /已完成/ })).getByText("1"),
    ).toBeTruthy();

    await waitFor(() => {
      expect(mocks.loadPendingCustomers).toHaveBeenCalledOnce();
      expect(mocks.loadDoneCustomers).toHaveBeenCalledOnce();
    });
  });

  it("规划数据就绪前不查询名单，就绪后补发请求", async () => {
    mocks.state.plansLoaded = false;

    const { rerender } = render(
      <MemoryRouter initialEntries={["/wealth/board"]}>
        <Sidebar collapsed={false} onToggleCollapse={vi.fn()} />
      </MemoryRouter>,
    );

    expect(mocks.loadPendingCustomers).not.toHaveBeenCalled();
    expect(mocks.loadDoneCustomers).not.toHaveBeenCalled();

    mocks.state.plansLoaded = true;
    rerender(
      <MemoryRouter initialEntries={["/wealth/board"]}>
        <Sidebar collapsed={false} onToggleCollapse={vi.fn()} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mocks.loadPendingCustomers).toHaveBeenCalledOnce();
      expect(mocks.loadDoneCustomers).toHaveBeenCalledOnce();
    });
  });
});
