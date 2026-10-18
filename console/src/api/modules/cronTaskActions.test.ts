import { beforeEach, expect, it, vi } from "vitest";
import { schedulerCronApi } from "./schedulerCron";
import { cronJobApi } from "./cronjob";
import { applyManagedTaskAction, markSourcedTaskRead } from "./cronTaskActions";
import { tagTask } from "./cronTaskSources";
import { currentCronContext } from "../cronTaskContext";
import { useIframeStore } from "@/stores/iframeStore";
import { useAgentStore } from "@/stores/agentStore";
vi.mock("./schedulerCron", () => ({
  schedulerCronApi: {
    markRead: vi.fn(),
    setEnabled: vi.fn(),
    run: vi.fn(),
    remove: vi.fn(),
  },
}));
vi.mock("./cronjob", () => ({ cronJobApi: { markTaskRead: vi.fn() } }));
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
const task = (owner: "legacy" | "scheduler") =>
  tagTask(
    {
      id: "same",
      name: "任务",
      definition_version: 2,
      management_version: 3,
    } as never,
    owner,
    currentCronContext(),
  );
it("新任务启停使用管理版本，执行使用定义版本，已读不恢复", async () => {
  const job = task("scheduler");
  await applyManagedTaskAction(job as never, "pause", "pause-id");
  expect(schedulerCronApi.setEnabled).toHaveBeenCalledWith(
    currentCronContext(),
    "same",
    false,
    3,
    "pause-id",
  );
  await applyManagedTaskAction(job as never, "run", "run-id");
  expect(schedulerCronApi.run).toHaveBeenCalledWith(
    currentCronContext(),
    "same",
    2,
    "run-id",
  );
  vi.mocked(schedulerCronApi.markRead).mockResolvedValue({
    operation: { status: "completed" },
  } as never);
  await markSourcedTaskRead(job, true, "read-id");
  expect(schedulerCronApi.markRead).toHaveBeenCalledWith(
    currentCronContext(),
    "same",
    "read-id",
  );
  expect(schedulerCronApi.setEnabled).toHaveBeenCalledTimes(1);
  expect(cronJobApi.markTaskRead).not.toHaveBeenCalled();
});
it("旧任务已读保留原协议，新接口失败不回退旧写接口", async () => {
  await markSourcedTaskRead(task("legacy"), true, "legacy-read");
  expect(cronJobApi.markTaskRead).toHaveBeenCalledWith("same", true);
  vi.mocked(schedulerCronApi.markRead).mockRejectedValue(
    new Error("new unavailable"),
  );
  await expect(
    markSourcedTaskRead(task("scheduler"), true, "new-read"),
  ).rejects.toThrow("new unavailable");
  expect(cronJobApi.markTaskRead).toHaveBeenCalledTimes(1);
});
it("Agent 或角色切换后旧任务对象不能继续写入", async () => {
  const job = task("scheduler");
  useAgentStore.setState({ selectedAgent: "b" });
  await expect(
    applyManagedTaskAction(job as never, "run", "late"),
  ).rejects.toThrow("工作区");
  expect(schedulerCronApi.run).not.toHaveBeenCalled();
});
