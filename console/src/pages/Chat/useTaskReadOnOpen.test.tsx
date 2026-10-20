import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useTaskReadOnOpen } from "./useTaskReadOnOpen";
import { markSourcedTaskRead } from "@/api/modules/cronTaskActions";
import { tagTask } from "@/api/modules/cronTaskSources";
import { currentCronContext } from "@/api/cronTaskContext";
import type { CronJobSpecOutput } from "@/api/types";
vi.mock("@/api/modules/cronTaskActions", () => ({
  markSourcedTaskRead: vi.fn(),
}));
afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());
const task = () =>
  tagTask(
    {
      id: "j",
      enabled: true,
      task: { unread_execution_count: 2, is_paused: false },
    } as never,
    "scheduler",
    currentCronContext(),
  );
it("已读失败不清理徽标，只报错；同一展示快照不反复发请求", async () => {
  vi.mocked(markSourcedTaskRead).mockRejectedValue(new Error("unavailable"));
  const changed = vi.fn(),
    failed = vi.fn(),
    job = task();
  const { rerender } = renderHook(
    ({ value }) => useTaskReadOnOpen(value, changed, failed),
    { initialProps: { value: job } },
  );
  await waitFor(() => expect(failed).toHaveBeenCalled());
  rerender({ value: { ...job } });
  expect(markSourcedTaskRead).toHaveBeenCalledTimes(1);
  expect(changed).not.toHaveBeenCalled();
  expect(job.task?.unread_execution_count).toBe(2);
});
it("暂停中的任务仅打开不标读，也不恢复；成功时才刷新实际水位", async () => {
  vi.mocked(markSourcedTaskRead).mockResolvedValue({});
  const changed = vi.fn().mockResolvedValue(undefined),
    failed = vi.fn();
  const job = task();
  const { rerender } = renderHook(
    ({ value }: { value: CronJobSpecOutput }) =>
      useTaskReadOnOpen(value, changed, failed),
    {
      initialProps: {
        value: {
          ...job,
          task: { ...job.task!, is_paused: true },
        } as CronJobSpecOutput,
      },
    },
  );
  expect(markSourcedTaskRead).not.toHaveBeenCalled();
  rerender({ value: job });
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
});
