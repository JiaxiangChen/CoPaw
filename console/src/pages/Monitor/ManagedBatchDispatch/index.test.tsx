import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ManagedBatchDispatchPage from "./index";

const api = vi.hoisted(() => ({
  batches: vi.fn(),
  detail: vi.fn(),
  workers: vi.fn(),
  failures: vi.fn(),
}));
const context = vi.hoisted(() => ({
  source: "bank",
  userId: "manager",
  manager: true,
  isSuperManager: false,
}));
vi.mock("../../../api/modules/managedBatchBoard", () => ({
  managedBatchBoard: api,
}));
vi.mock("../../../stores/iframeStore", () => ({
  useIframeStore: (selector: (state: typeof context) => unknown) =>
    selector(context),
}));
vi.mock("../CronBatchDispatch/WorkerHistoryChart", () => ({
  default: () => <div>测试容量曲线</div>,
}));
beforeEach(() => {
  vi.clearAllMocks();
  context.manager = true;
  api.batches.mockResolvedValue({
    items: [],
    total: 0,
    stats: {
      total_batches: 0,
      total_members: 0,
      done_members: 0,
      failed_members: 0,
      running_batches: 0,
    },
  });
  api.workers.mockResolvedValue({
    source_id: "bank",
    policies: [],
    current_capacity: [],
    capacity_events: [],
    capacity_events_next_cursor: null,
  });
});
afterEach(cleanup);
it("非管理员不加载跨用户看板数据", () => {
  context.manager = false;
  render(<ManagedBatchDispatchPage />);
  expect(screen.getByText("仅管理员可访问批调度看板（新）")).toBeTruthy();
  expect(api.batches).not.toHaveBeenCalled();
  expect(api.workers).not.toHaveBeenCalled();
});
it("零成员不伪造百分之百完成率，空数据没有旧接口回退", async () => {
  render(<ManagedBatchDispatchPage />);
  await waitFor(() => expect(api.batches).toHaveBeenCalled());
  const overview = screen.getByRole("region", { name: "新版批调度概览" });
  await waitFor(() =>
    expect(
      within(overview).getByText("成员完成率").parentElement?.textContent,
    ).toContain("-"),
  );
  expect(within(overview).queryByText("100%")).toBeNull();
  expect(await screen.findByText("当前范围内暂无 Batch")).toBeTruthy();
  expect(api.detail).not.toHaveBeenCalled();
});
it("共享容量中途失败明确标记不完整，不显示成功曲线", async () => {
  api.workers
    .mockResolvedValueOnce({
      source_id: "bank",
      policies: [],
      current_capacity: [],
      capacity_events: [],
      capacity_events_next_cursor: "next-page",
    })
    .mockRejectedValueOnce(new Error("history_unavailable"));
  render(<ManagedBatchDispatchPage />);
  expect(
    await screen.findByText("容量历史不完整：history_unavailable"),
  ).toBeTruthy();
  expect(screen.queryByText("测试容量曲线")).toBeNull();
});
