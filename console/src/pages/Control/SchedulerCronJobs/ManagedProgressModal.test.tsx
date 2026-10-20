import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import { ManagedProgressModal } from "./ManagedProgressModal";
vi.mock("@/api/modules/schedulerCron", () => ({
  schedulerCronApi: { pending: vi.fn(), execution: vi.fn() },
}));
afterEach(cleanup);
it("主执行成功但异步失败，分别展示两个真实结果，不伪装整轮成功", async () => {
  const execution = {
    execution_id: "e",
    job_id: "j",
    status: "success",
    settled_status: "error",
    settled_error_code: "cron_async_subtasks_failed",
  };
  vi.mocked(schedulerCronApi.pending).mockResolvedValue({ operation: null });
  vi.mocked(schedulerCronApi.execution).mockResolvedValue(execution);
  render(
    <ManagedProgressModal
      context={{ actor_id: "u", tenant_id: "u", source_id: "s", agent_id: "a" }}
      job={{ id: "j", name: "任务" } as never}
      initialExecution={execution}
      onClose={() => {}}
      onChanged={() => {}}
    />,
  );
  expect(await screen.findByText("整轮结果")).toBeInTheDocument();
  expect(
    await screen.findByText("cron_async_subtasks_failed"),
  ).toBeInTheDocument();
  expect(screen.getByText("success")).toBeInTheDocument();
  expect(screen.getByText("error")).toBeInTheDocument();
});

it("旧响应未提供收敛字段，不假报等待异步结果", async () => {
  const execution = {
    execution_id: "old-protocol",
    job_id: "j",
    status: "success",
  };
  vi.mocked(schedulerCronApi.pending).mockResolvedValue({ operation: null });
  vi.mocked(schedulerCronApi.execution).mockResolvedValue(execution);
  render(
    <ManagedProgressModal
      context={{ actor_id: "u", tenant_id: "u", source_id: "s", agent_id: "a" }}
      job={{ id: "j", name: "任务" } as never}
      initialExecution={execution}
      onClose={() => {}}
      onChanged={() => {}}
    />,
  );
  await screen.findByText("success");
  expect(screen.queryByText("等待异步结果确认")).toBeNull();
});
