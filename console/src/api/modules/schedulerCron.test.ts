import { beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../request";
import { schedulerCronApi, targetManagedContext } from "./schedulerCron";

vi.mock("../request", () => ({ request: vi.fn() }));
const context = {
  actor_id: "alice",
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
};

it("日期修改和整组暂停使用独立新接口与对应版本", async () => {
  vi.mocked(request).mockResolvedValue({ operation: { status: "completed" } });
  const period = { start_date: "2026-11-01", end_date: "2026-11-30" };
  await schedulerCronApi.setDateRange(context, "root", period, 8, "dates-1");
  const [path, options] =
    vi.mocked(request).mock.calls[vi.mocked(request).mock.calls.length - 1];
  expect(path).toBe("/scheduler/cron/jobs/root/run-date-range");
  expect(JSON.parse(String(options?.body))).toEqual({
    request_id: "dates-1",
    expected_management_version: 8,
    run_date_range: period,
  });
  await schedulerCronApi.pause(context, "root", 3, "pause-1");
  const [pausePath, pauseOptions] =
    vi.mocked(request).mock.calls[vi.mocked(request).mock.calls.length - 1];
  expect(pausePath).toBe("/scheduler/cron/jobs/root/pause");
  expect(JSON.parse(String(pauseOptions?.body))).toEqual({
    request_id: "pause-1",
    expected_definition_version: 3,
  });
});

describe("新版任务请求边界", () => {
  beforeEach(() => vi.clearAllMocks());

  it("完整遍历游标分页，不截断为第一页", async () => {
    vi.mocked(request)
      .mockResolvedValueOnce({ items: [{ id: "one" }], next_cursor: "next" })
      .mockResolvedValueOnce({ items: [{ id: "two" }], next_cursor: null });
    await expect(schedulerCronApi.listAll(context)).resolves.toEqual([
      { id: "one" },
      { id: "two" },
    ]);
    expect(vi.mocked(request).mock.calls[1][0]).toContain("cursor=next");
    expect(
      vi
        .mocked(request)
        .mock.calls.every(([path]) => path.startsWith("/scheduler/cron/jobs")),
    ).toBe(true);
  });

  it("目标工作区切换不冒充接收人或提升角色", async () => {
    vi.mocked(request).mockResolvedValue({
      operation: { status: "completed" },
    });
    const child = targetManagedContext(context, {
      tenant_id: "bob",
      source_id: "bank",
      agent_id: "agent-b",
    });
    await schedulerCronApi.setEnabled(
      child,
      "job-b",
      false,
      9,
      "stable-request",
    );
    const [, options] = vi.mocked(request).mock.calls[0];
    const headers = new Headers(options?.headers);
    expect(headers.get("X-User-Id")).toBe("alice");
    expect(headers.get("X-Tenant-Id")).toBe("bob");
    expect(headers.get("X-Agent-Id")).toBe("agent-b");
    expect(headers.has("X-User-Role")).toBe(false);
    expect(JSON.parse(String(options?.body))).toEqual({
      request_id: "stable-request",
      enabled: false,
      expected_management_version: 9,
    });
  });

  it("分发只带散列和目标，不携带调度模式", async () => {
    vi.mocked(request).mockResolvedValue({ task_id: "task" });
    await schedulerCronApi.broadcast(context, "root", {
      request_id: "round",
      expected_definition_version: 3,
      targets: [],
      enable_offset: true,
      offset_window_hours: 4,
    });
    const [path, options] = vi.mocked(request).mock.calls[0];
    expect(path).toBe("/scheduler/cron/jobs/root/broadcast");
    const body = JSON.parse(String(options?.body));
    expect(body.targets).toEqual([]);
    expect(body).not.toHaveProperty("enable_batch_dispatch");
    expect(body).not.toHaveProperty("dispatch_state");
  });

  it("新接口失败直接抛错，不探测或回退旧接口", async () => {
    vi.mocked(request).mockRejectedValue(new Error("new service unavailable"));
    await expect(
      schedulerCronApi.markRead(context, "job", "read-request"),
    ).rejects.toThrow("new service unavailable");
    expect(request).toHaveBeenCalledTimes(1);
    expect(vi.mocked(request).mock.calls[0][0]).toBe(
      "/scheduler/cron/jobs/job/read",
    );
  });

  it("重复分页游标明确失败，不死循环或交付不完整列表", async () => {
    vi.mocked(request).mockResolvedValue({ items: [], next_cursor: "same" });
    await expect(schedulerCronApi.listAll(context)).rejects.toThrow("分页");
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("创建剔除服务器 ID 和展示字段，不删业务输入或元数据", async () => {
    vi.mocked(request).mockResolvedValue({
      operation: { status: "completed" },
    });
    await schedulerCronApi.create(
      context,
      {
        id: "由系统分配",
        name: "job",
        schedule: { type: "cron", cron: "0 9 * * *" },
        dispatch: { type: "channel", target: { user_id: "", session_id: "" } },
        request: { input: { token: "business-value" } },
        meta: { business: "keep" },
        run_date_range: {
          start_date: "2026-11-01",
          end_date: "2026-11-30",
        },
        state: { last_status: "success" },
        management_version: 9,
      } as Parameters<typeof schedulerCronApi.create>[1],
      "stable-create",
    );
    const body = JSON.parse(String(vi.mocked(request).mock.calls[0][1]?.body));
    expect(body.spec).not.toHaveProperty("id");
    expect(body.spec).not.toHaveProperty("state");
    expect(body.spec).not.toHaveProperty("management_version");
    expect(body.spec.request.input.token).toBe("business-value");
    expect(body.spec.meta.business).toBe("keep");
    expect(body.spec.run_date_range).toEqual({
      start_date: "2026-11-01",
      end_date: "2026-11-30",
    });
  });
});
