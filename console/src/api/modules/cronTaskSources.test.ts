import { beforeEach, describe, expect, it, vi } from "vitest";
import { cronJobApi } from "./cronjob";
import { schedulerCronApi } from "./schedulerCron";
import {
  listTaskSources,
  mergeTaskSources,
  taskIdentityKey,
  resolveTaskOrigin,
  tagTask,
} from "./cronTaskSources";
import { request } from "../request";
import type { ManagedJob } from "../types/schedulerCron";

vi.mock("./cronjob", () => ({ cronJobApi: { listCronJobs: vi.fn() } }));
vi.mock("./schedulerCron", () => ({ schedulerCronApi: { listAll: vi.fn() } }));
vi.mock("../request", () => ({ request: vi.fn() }));
const context = {
  actor_id: "u",
  tenant_id: "u",
  source_id: "s",
  agent_id: "a",
};
const legacy = { id: "same", name: "旧任务" } as ManagedJob;
const managed = {
  ...legacy,
  name: "新任务",
  management_owner: "scheduler",
  definition_version: 2,
  management_version: 3,
  ...context,
} as ManagedJob;
beforeEach(() => vi.clearAllMocks());

describe("Chat 任务来源", () => {
  it("并行读取两份列表，旧在前新在后，同 ID 不合并", async () => {
    vi.mocked(cronJobApi.listCronJobs).mockResolvedValue([legacy]);
    vi.mocked(schedulerCronApi.listAll).mockResolvedValue([managed]);
    const sources = await listTaskSources(context);
    const jobs = mergeTaskSources([], sources);
    expect(jobs.map((job) => job.name)).toEqual(["旧任务", "新任务"]);
    expect(taskIdentityKey(jobs[0])).not.toBe(taskIdentityKey(jobs[1]));
    expect(schedulerCronApi.listAll).toHaveBeenCalledWith(
      context,
      expect.anything(),
    );
  });
  it("一个来源失败时保留其已知任务，不影响另一来源更新", async () => {
    const previous = [
      tagTask(legacy, "legacy", context),
      tagTask(managed, "scheduler", context),
    ];
    vi.mocked(cronJobApi.listCronJobs).mockRejectedValue(
      new Error("old unavailable"),
    );
    vi.mocked(schedulerCronApi.listAll).mockResolvedValue([
      { ...managed, name: "新结果" },
    ]);
    const sources = await listTaskSources(context);
    expect(sources.legacy).toBeNull();
    expect(sources.errors).toContain("旧任务列表暂不可用，保留已知任务");
    expect(mergeTaskSources(previous, sources).map((job) => job.name)).toEqual([
      "旧任务",
      "新结果",
    ]);
  });
  it("新版返回 legacy 快照或不完整的管理身份时不能打成新版来源", async () => {
    vi.mocked(cronJobApi.listCronJobs).mockResolvedValue([]);
    vi.mocked(schedulerCronApi.listAll).mockResolvedValue([
      { ...managed, management_owner: "legacy" } as never,
    ]);
    const sources = await listTaskSources(context);
    expect(sources.scheduler).toBeNull();
    expect(sources.errors).toHaveLength(1);
  });
});

describe("报告只有任务 ID 时只读确认归属", () => {
  it("旧详情 404、新详情为 scheduler 才确认新归属", async () => {
    vi.mocked(request).mockImplementation(async (path) => {
      if (path.startsWith("/cron/"))
        throw Object.assign(new Error("not found"), { status: 404 });
      return {
        management_owner: "scheduler",
        agent_id: "a",
        spec: { id: "same", tenant_id: "u", source_id: "s" },
      };
    });
    await expect(resolveTaskOrigin(context, "same")).resolves.toMatchObject({
      owner: "scheduler",
      context,
    });
    expect(
      vi
        .mocked(request)
        .mock.calls.every(
          ([, options]) => !options?.method || options.method === "GET",
        ),
    ).toBe(true);
  });
  it("双方都存在或一方查询失败均不能用写接口探测归属", async () => {
    vi.mocked(request).mockResolvedValue({
      management_owner: "scheduler",
      agent_id: "a",
      spec: { id: "same", tenant_id: "u", source_id: "s" },
    });
    await expect(resolveTaskOrigin(context, "same")).rejects.toThrow(
      "归属不唯一",
    );
    vi.mocked(request).mockImplementation(async (path) => {
      if (path.startsWith("/scheduler/"))
        throw Object.assign(new Error("unavailable"), { status: 503 });
      return { spec: { id: "same" } };
    });
    await expect(resolveTaskOrigin(context, "same")).rejects.toThrow(
      "无法确认",
    );
    expect(
      vi.mocked(request).mock.calls.every(([, options]) => !options?.method),
    ).toBe(true);
  });
});
