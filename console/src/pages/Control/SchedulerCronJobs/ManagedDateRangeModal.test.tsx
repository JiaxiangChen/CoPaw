import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import type { ManagedJob } from "@/api/types/schedulerCron";
import { ManagedDateRangeModal } from "./ManagedDateRangeModal";

vi.mock("@/api/modules/schedulerCron", () => ({
  schedulerCronApi: { detail: vi.fn(), setDateRange: vi.fn() },
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
const context = {
  actor_id: "alice",
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
};
const job = {
  id: "root",
  name: "父任务",
  management_version: 2,
  meta: {},
} as ManagedJob;

it("读取最新父任务日期与管理版本，清空后调用独立接口且不重新分发", async () => {
  vi.mocked(schedulerCronApi.detail).mockResolvedValue({
    spec: {
      ...job,
      run_date_range: { start_date: "2026-11-01", end_date: "2026-11-30" },
    },
    management_version: 8,
  } as never);
  vi.mocked(schedulerCronApi.setDateRange).mockResolvedValue({
    operation: { status: "completed" },
  } as never);
  const changed = vi.fn(),
    close = vi.fn();
  render(
    <ManagedDateRangeModal
      context={context}
      job={job}
      onChanged={changed}
      onClose={close}
    />,
  );
  await screen.findByDisplayValue("2026-11-01");
  fireEvent.click(screen.getByRole("button", { name: "清空区间" }));
  fireEvent.click(screen.getByRole("button", { name: "保存区间" }));
  await waitFor(() =>
    expect(schedulerCronApi.setDateRange).toHaveBeenCalledWith(
      context,
      "root",
      null,
      8,
      expect.any(String),
    ),
  );
  expect(changed).toHaveBeenCalledTimes(1);
  expect(close).toHaveBeenCalledTimes(1);
});

it("子任务只读展示继承日期，不能通过弹窗提交修改", async () => {
  vi.mocked(schedulerCronApi.detail).mockResolvedValue({
    spec: {
      ...job,
      meta: { broadcast_source_job_id: "parent" },
      run_date_range: { start_date: "2026-11-01", end_date: "2026-11-30" },
    },
    management_version: 8,
  } as never);
  render(
    <ManagedDateRangeModal
      context={context}
      job={job}
      onChanged={() => {}}
      onClose={() => {}}
    />,
  );
  await screen.findByText(/继承父任务/);
  expect(screen.getByRole("button", { name: "保存区间" })).toBeDisabled();
  expect(schedulerCronApi.setDateRange).not.toHaveBeenCalled();
});

it("详情加载失败时不能把未知配置误清空", async () => {
  vi.mocked(schedulerCronApi.detail).mockRejectedValue(
    new Error("详情读取失败"),
  );
  render(
    <ManagedDateRangeModal
      context={context}
      job={job}
      onChanged={() => {}}
      onClose={() => {}}
    />,
  );
  await screen.findByText("详情读取失败");
  expect(screen.getByRole("button", { name: "保存区间" })).toBeDisabled();
  expect(schedulerCronApi.setDateRange).not.toHaveBeenCalled();
});
