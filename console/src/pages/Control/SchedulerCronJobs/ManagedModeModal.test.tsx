import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import { ManagedModeModal } from "./ManagedModeModal";
import type { ManagedJob } from "@/api/types/schedulerCron";
vi.mock("../CronJobs/components/BatchPriorityEditor", () => ({
  default: () => <div>优先策略</div>,
}));
vi.mock("@/api/modules/schedulerCron", () => ({
  schedulerCronApi: {
    dispatchState: vi.fn(),
    priority: vi.fn(),
    setDispatchState: vi.fn(),
    retry: vi.fn(),
  },
}));
const context = {
  actor_id: "u",
  tenant_id: "u",
  source_id: "s",
  agent_id: "a",
};
const job = { id: "j", name: "任务" } as ManagedJob;
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(schedulerCronApi.priority).mockResolvedValue({
    priority: { user_ids: [], branch_ids: [] },
    management_version: 1,
  });
});
it("切换未完成或外部失败不能换目标，只能刷新或重试", async () => {
  vi.mocked(schedulerCronApi.dispatchState).mockResolvedValue({
    job_id: "j",
    state: "batch_enabled",
    sync_status: "failed",
    operation_id: "op",
    version: 2,
    effective_state: "mixed",
  } as never);
  render(
    <ManagedModeModal
      context={context}
      job={job}
      onClose={() => {}}
      onChanged={() => {}}
    />,
  );
  await screen.findByText(/实际生效.*混合/);
  expect(screen.getByRole("button", { name: "提交切换" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "重试原切换" }));
  await waitFor(() =>
    expect(schedulerCronApi.retry).toHaveBeenCalledWith(context, "j", "op"),
  );
  expect(schedulerCronApi.setDispatchState).not.toHaveBeenCalled();
});
