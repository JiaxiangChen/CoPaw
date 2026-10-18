import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useManagedTaskActions } from "./useManagedTaskActions";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import { tagTask } from "@/api/modules/cronTaskSources";
import { cronAccessKey, currentCronContext } from "@/api/cronTaskContext";
import { useIframeStore } from "@/stores/iframeStore";
import { useAgentStore } from "@/stores/agentStore";
vi.mock("@/api/modules/schedulerCron", () => ({
  schedulerCronApi: { run: vi.fn() },
}));
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  useIframeStore.setState({
    userId: "u",
    source: "s",
    manager: false,
    isSuperManager: false,
  });
  useAgentStore.setState({ selectedAgent: "a" });
});
const job = () =>
  tagTask(
    {
      id: "j",
      management_owner: "scheduler",
      definition_version: 2,
      management_version: 3,
    } as never,
    "scheduler",
    currentCronContext(),
  ) as never;
it("新任务执行只展示真实受理状态，不给列表伪造 running", async () => {
  const changed = vi.fn().mockResolvedValue(undefined),
    notify = vi.fn();
  vi.mocked(schedulerCronApi.run).mockResolvedValue({
    execution: { execution_id: "e", job_id: "j", status: "queued" },
    reused: false,
  });
  const { result } = renderHook(() =>
    useManagedTaskActions(cronAccessKey(), changed, notify),
  );
  await act(async () => {
    await result.current.perform(job(), "run");
  });
  expect(result.current.progress?.execution?.status).toBe("queued");
  expect(notify).toHaveBeenCalledWith(expect.stringContaining("已受理"), false);
  expect(changed).toHaveBeenCalledOnce();
});
it("网络结果未知时重试使用原 request_id", async () => {
  vi.mocked(schedulerCronApi.run).mockRejectedValue(new Error("timeout"));
  const { result } = renderHook(() =>
    useManagedTaskActions(
      cronAccessKey(),
      async () => {},
      () => {},
    ),
  );
  const task = job();
  await act(async () => {
    await result.current.perform(task, "run");
  });
  await act(async () => {
    await result.current.perform(task, "run");
  });
  expect(vi.mocked(schedulerCronApi.run).mock.calls[0][3]).toBe(
    vi.mocked(schedulerCronApi.run).mock.calls[1][3],
  );
});
it("新工作区不显示旧执行迟到结果，旧动作不能重新发送", async () => {
  let resolve!: (value: never) => void;
  vi.mocked(schedulerCronApi.run).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const notify = vi.fn();
  const { result, rerender } = renderHook(
    ({ key }) => useManagedTaskActions(key, async () => {}, notify),
    { initialProps: { key: cronAccessKey() } },
  );
  const task = job();
  let pending!: Promise<unknown>;
  act(() => {
    pending = result.current.perform(task, "run");
  });
  useAgentStore.setState({ selectedAgent: "b" });
  rerender({ key: cronAccessKey() });
  await act(async () => {
    resolve({
      execution: { execution_id: "late", status: "running" },
    } as never);
    await pending;
  });
  expect(result.current.progress).toBeNull();
  expect(notify).not.toHaveBeenCalled();
  await act(async () => {
    await result.current.perform(task, "run");
  });
  expect(schedulerCronApi.run).toHaveBeenCalledTimes(1);
});

it("Agent 切走再切回后，旧动作也不能覆盖新一轮进度或解锁新请求", async () => {
  let resolveOld!: (value: never) => void;
  vi.mocked(schedulerCronApi.run)
    .mockReturnValueOnce(
      new Promise((done) => {
        resolveOld = done;
      }),
    )
    .mockResolvedValueOnce({
      execution: { execution_id: "current", job_id: "j", status: "queued" },
      reused: false,
    });
  const changed = vi.fn().mockResolvedValue(undefined),
    notify = vi.fn();
  const { result, rerender } = renderHook(
    ({ key }) => useManagedTaskActions(key, changed, notify),
    { initialProps: { key: cronAccessKey() } },
  );
  const captured = job();
  let old!: Promise<unknown>;
  act(() => {
    old = result.current.perform(captured, "run");
  });
  useAgentStore.setState({ selectedAgent: "temporary" });
  rerender({ key: cronAccessKey() });
  useAgentStore.setState({ selectedAgent: "a" });
  rerender({ key: cronAccessKey() });
  await act(async () => {
    await result.current.perform(job(), "run");
  });
  await act(async () => {
    resolveOld({
      execution: { execution_id: "old", status: "running" },
    } as never);
    await old;
  });
  expect(result.current.progress?.execution?.execution_id).toBe("current");
  expect(changed).toHaveBeenCalledTimes(1);
});
