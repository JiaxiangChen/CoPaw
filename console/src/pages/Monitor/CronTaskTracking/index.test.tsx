import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import CronTaskTracking from "./index";

const api = vi.hoisted(() => ({
  lookup: vi.fn(),
  overview: vi.fn(),
  timeline: vi.fn(),
}));
const context = vi.hoisted(() => ({
  source: "bank",
  userId: "admin",
  manager: true,
  isSuperManager: false,
}));
vi.mock("../../../api/modules/cronTaskTracking", () => ({
  cronTaskTracking: api,
}));
vi.mock("../../../stores/iframeStore", () => ({
  useIframeStore: (select: (value: typeof context) => unknown) =>
    select(context),
}));
const identity = {
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
  job_id: "job-1",
};
const metadata = {
  coverage: {},
  warnings: [],
  queried_at: "2026-10-27T10:00:00+08:00",
  query_id: "query",
};
const overview = {
  identity,
  relation: "parent",
  definition: {
    name: "任务甲",
    cron_expr: "0 10 * * *",
    timezone: "Asia/Shanghai",
  },
  state: { last_status: "success" },
  history_only: false,
  ...metadata,
};
const node = {
  event_id: "event-1",
  node_type: "E09",
  category: "execution",
  title: "Scheduler 已确认整轮结果",
  occurred_at: "2026-10-27T10:00:00+08:00",
  time_basis: "scheduler_confirmed",
  recorded_outcome: "error",
  entry: "batch",
  reason: "recorded_reason",
  details: { error_message: "<script>bad()</script>" },
  source_refs: [],
  gaps: [],
};
const timeline = {
  identity,
  items: [node],
  has_more: false,
  next_cursor: null,
  loaded_count: 1,
  ...metadata,
};

beforeEach(() => {
  vi.clearAllMocks();
  context.manager = true;
  api.lookup.mockResolvedValue({
    candidates: [{ ...identity, name: "任务甲" }],
    has_more: false,
    requires_selection: false,
    ...metadata,
  });
  api.overview.mockResolvedValue(overview);
  api.timeline.mockResolvedValue(timeline);
});
afterEach(cleanup);

function search(id = "job-1") {
  fireEvent.change(screen.getByRole("textbox", { name: "任务 ID" }), {
    target: { value: id },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询任务" }));
}

it("does not query any data when role is not manager", () => {
  context.manager = false;
  render(<CronTaskTracking />);
  expect(screen.getByText("仅管理员可访问定时任务追踪")).toBeTruthy();
  expect(api.lookup).not.toHaveBeenCalled();
});

it("shows current overview and confirmation timeline without marking results read", async () => {
  render(<CronTaskTracking />);
  search();
  expect(await screen.findByText("任务甲")).toBeTruthy();
  expect(await screen.findByText("Scheduler 已确认整轮结果")).toBeTruthy();
  expect(screen.getByText("父任务")).toBeTruthy();
  expect(
    screen.getByRole("textbox", { name: "开始日期（北京时间）" }),
  ).toBeTruthy();
  expect(
    screen.getByRole("textbox", { name: "结束日期（北京时间）" }),
  ).toBeTruthy();
  expect(screen.getByText("确认时间，非异步实际完成时间")).toBeTruthy();
  fireEvent.click(screen.getByText("查看详情"));
  expect(await screen.findByText(/<script>bad\(\)<\/script>/)).toBeTruthy();
  expect(document.querySelector("script")).toBeNull();
});

it("requires a deliberate choice for multiple identities", async () => {
  api.lookup.mockResolvedValue({
    candidates: [identity, { ...identity, tenant_id: "bob" }],
    requires_selection: true,
    has_more: false,
    ...metadata,
  });
  render(<CronTaskTracking />);
  search();
  expect(await screen.findByText("请选择任务归属")).toBeTruthy();
  expect(api.overview).not.toHaveBeenCalled();
  expect(api.timeline).not.toHaveBeenCalled();
});

it("ignores late lookup completion from a previously queried task", async () => {
  let finish: (value: unknown) => void = () => undefined;
  api.lookup.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  render(<CronTaskTracking />);
  search("old");
  search("missing");
  api.lookup.mockResolvedValue({
    candidates: [],
    has_more: false,
    requires_selection: true,
    ...metadata,
  });
  await waitFor(() => expect(api.lookup).toHaveBeenCalledTimes(2));
  await act(async () =>
    finish({
      candidates: [{ ...identity, job_id: "old" }],
      requires_selection: false,
      has_more: false,
      ...metadata,
    }),
  );
  expect(api.overview.mock.calls.some((args) => args[1].job_id === "old")).toBe(
    false,
  );
});

it("changes filters and paginates through only the new read-only endpoint", async () => {
  api.timeline
    .mockResolvedValueOnce({
      ...timeline,
      has_more: true,
      next_cursor: "cursor-1",
    })
    .mockResolvedValueOnce({ ...timeline, items: [] });
  render(<CronTaskTracking />);
  search();
  const next = await screen.findByRole("button", { name: "下一页" });
  await waitFor(() => expect(next).not.toBeDisabled());
  fireEvent.click(next);
  await waitFor(() =>
    expect(
      api.timeline.mock.calls[api.timeline.mock.calls.length - 1]?.[2].cursor,
    ).toBe("cursor-1"),
  );
  fireEvent.click(screen.getByRole("radio", { name: "管理操作" }));
  await waitFor(() =>
    expect(
      api.timeline.mock.calls[api.timeline.mock.calls.length - 1]?.[2].category,
    ).toBe("management"),
  );
  expect(
    api.timeline.mock.calls[api.timeline.mock.calls.length - 1]?.[2].cursor,
  ).toBeUndefined();
});

it("keeps the overview when timeline fails and supports a read-only retry", async () => {
  api.timeline
    .mockRejectedValueOnce(new Error("cron_tracking_query_failed"))
    .mockResolvedValueOnce(timeline);
  render(<CronTaskTracking />);
  search();
  expect(await screen.findByText("任务甲")).toBeTruthy();
  expect(await screen.findByText("时间轴加载失败")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "重试查询" }));
  expect(await screen.findByText("Scheduler 已确认整轮结果")).toBeTruthy();
});

it("refreshes both current overview and timeline without reusing the next-page cursor", async () => {
  render(<CronTaskTracking />);
  search();
  expect(await screen.findByText("Scheduler 已确认整轮结果")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await waitFor(() => expect(api.overview).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(api.timeline).toHaveBeenCalledTimes(2));
  expect(api.timeline.mock.calls[1][2].cursor).toBeUndefined();
});

it("requires refinement instead of choosing from truncated identities", async () => {
  api.lookup.mockResolvedValue({
    candidates: [identity],
    requires_selection: true,
    has_more: true,
    ...metadata,
  });
  render(<CronTaskTracking />);
  search();
  expect(
    await screen.findByText(
      "候选超过 20 项，请填写用户 ID 或 Agent 后重新查询",
    ),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "alice / default" }),
  ).toBeDisabled();
  expect(api.overview).not.toHaveBeenCalled();
});
