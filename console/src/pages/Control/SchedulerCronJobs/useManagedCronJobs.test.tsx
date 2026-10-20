import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import { useManagedCronJobs } from "./useManagedCronJobs";
import type { ManagedJob } from "@/api/types/schedulerCron";

vi.mock("@/api/modules/schedulerCron", () => ({
  targetManagedContext: (ctx: object, target: object) => ({
    ...ctx,
    ...target,
  }),
  schedulerCronApi: {
    listAll: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    setEnabled: vi.fn(),
    run: vi.fn(),
    retry: vi.fn(),
  },
}));
const context = {
  actor_id: "alice",
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
};
const job = {
  id: "job",
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
  enabled: true,
  definition_version: 1,
  management_version: 2,
  task: { is_running: false, is_paused: false },
} as ManagedJob;
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(schedulerCronApi.listAll).mockResolvedValue([job]);
});

it("详情对齐不覆盖其他Agent同名ID或更高版本，卸载后不再更新", async () => {
  const newer = { ...job, definition_version: 3 };
  vi.mocked(schedulerCronApi.listAll).mockResolvedValue([newer]);
  const { result, unmount } = renderHook(() => useManagedCronJobs(context));
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  act(() => result.current.reconcileJob({ ...job, definition_version: 2 }));
  expect(result.current.jobs[0]).toEqual(newer);
  act(() =>
    result.current.reconcileJob({
      ...job,
      agent_id: "other",
      definition_version: 4,
    }),
  );
  expect(result.current.jobs[0]).toEqual(newer);
  act(() => result.current.reconcileJob({ ...newer, name: "最新详情" }));
  expect(result.current.jobs[0].name).toBe("最新详情");
  const reconcile = result.current.reconcileJob;
  unmount();
  act(() => reconcile({ ...newer, name: "迟到详情" }));
  expect(result.current.jobs[0].name).toBe("最新详情");
});

it("Agent 切换后迟到旧响应不覆盖新列表", async () => {
  let resolveOld!: (value: ManagedJob[]) => void;
  vi.mocked(schedulerCronApi.listAll)
    .mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    )
    .mockResolvedValueOnce([
      { ...job, id: "new-agent-job", agent_id: "agent-b" },
    ]);
  const { result, rerender } = renderHook(
    ({ ctx }) => useManagedCronJobs(ctx),
    { initialProps: { ctx: context } },
  );
  rerender({ ctx: { ...context, agent_id: "agent-b" } });
  await waitFor(() => expect(result.current.jobs[0]?.id).toBe("new-agent-job"));
  await act(async () => {
    resolveOld([{ ...job, id: "old-agent-job" }]);
  });
  expect(result.current.jobs[0].id).toBe("new-agent-job");
});

it("只读刷新失败保留本来源已知列表并明确报错", async () => {
  const { result } = renderHook(() => useManagedCronJobs(context));
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  vi.mocked(schedulerCronApi.listAll).mockRejectedValueOnce(
    new Error("query unavailable"),
  );
  await act(async () => {
    await result.current.reload();
  });
  expect(result.current.jobs).toHaveLength(1);
  expect(result.current.error).toBe("query unavailable");
});

it("手动执行只显示实际受理状态，不乐观伪造 running", async () => {
  vi.mocked(schedulerCronApi.run).mockResolvedValue({
    execution: { execution_id: "run", job_id: job.id, status: "queued" },
    reused: false,
  });
  const { result } = renderHook(() => useManagedCronJobs(context));
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  await act(async () => {
    await result.current.run(job, "stable-click");
  });
  expect(result.current.execution?.status).toBe("queued");
  expect(result.current.jobs[0].task?.is_running).toBe(false);
});

it("页面已卸载时，旧确认框不能再发起执行", async () => {
  const { result, unmount } = renderHook(() => useManagedCronJobs(context));
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  const oldRun = result.current.run;
  unmount();
  await act(async () => {
    await oldRun(job, "late-confirm");
  });
  expect(schedulerCronApi.run).not.toHaveBeenCalled();
});

it("失败响应中的操作进度保留，用原操作续办", async () => {
  const operation = {
    operation_id: "op",
    job_id: job.id,
    status: "failed",
    action: "update",
    stage: "platform",
    version: 2,
    expected_version: 1,
    logical_delete_applied: false,
  } as const;
  vi.mocked(schedulerCronApi.update).mockRejectedValue(
    Object.assign(new Error("partial failure"), { data: { operation } }),
  );
  const { result } = renderHook(() => useManagedCronJobs(context));
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  await act(async () => {
    await result.current.update(
      job,
      { name: "changed" } as never,
      "stable-edit",
    );
  });
  expect(result.current.operation).toEqual(operation);
  expect(result.current.error).toBe("partial failure");
});
