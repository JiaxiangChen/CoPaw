import { beforeEach, expect, it, vi } from "vitest";
import { request } from "@/api/request";
import type { ManagedSelectedChild } from "@/api/types/schedulerCron";
import {
  createChildrenDeletion,
  runChildrenDeletion,
} from "./childrenDeletion";

vi.mock("@/api/request", () => ({ request: vi.fn() }));
const context = {
  actor_id: "owner",
  tenant_id: "owner",
  source_id: "bank",
  agent_id: "default",
};
const targets = (count: number): ManagedSelectedChild[] =>
  Array.from({ length: count }, (_, index) => ({
    job_id: `child-${index}`,
    tenant_id: `user-${index}`,
    agent_id: "default",
    expected_definition_version: 1,
  }));
beforeEach(() => vi.clearAllMocks());

it("全部1001项一次异步受理，网络未知重提原快照而不前端分片", async () => {
  const items = targets(1001);
  const plan = createChildrenDeletion(items, true);
  items[1000].expected_definition_version = 99;
  vi.mocked(request).mockRejectedValueOnce(new Error("网络结果未知"));
  await expect(
    runChildrenDeletion(
      context,
      "parent",
      plan,
      () => {},
      () => true,
    ),
  ).rejects.toThrow("网络结果未知");
  const original = JSON.parse(
    String(vi.mocked(request).mock.calls[0][1]?.body),
  );
  vi.mocked(request).mockResolvedValue({
    operation_id: "delete-op",
    operation_status: "pending",
    total_count: 1001,
    completed_count: 0,
    pending_count: 1001,
    results: [],
  });
  const result = await runChildrenDeletion(
    context,
    "parent",
    plan,
    () => {},
    () => true,
  );
  expect(result).toMatchObject({
    operation_id: "delete-op",
    operation_status: "pending",
    pending_count: 1001,
  });
  const requests = vi.mocked(request).mock.calls;
  expect(requests).toHaveLength(2);
  expect(requests[0][0]).toBe(
    "/scheduler/cron/jobs/parent/broadcast/children/delete-all",
  );
  expect(JSON.parse(String(requests[1][1]?.body))).toEqual(original);
  expect(original.items).toHaveLength(1001);
  expect(original.items[1000].expected_definition_version).toBe(1);
});

it("选中删除保持1000限制，受理后返回而不等待任务完成", async () => {
  expect(() => createChildrenDeletion(targets(1001))).toThrow();
  vi.mocked(request).mockResolvedValue({
    operation_id: "op",
    operation_status: "running",
    pending_count: 2,
    results: [],
  });
  const result = await runChildrenDeletion(
    context,
    "parent",
    createChildrenDeletion(targets(2)),
    () => {},
    () => true,
  );
  expect(result.operation_status).toBe("running");
  expect(vi.mocked(request).mock.calls).toHaveLength(1);
  expect(vi.mocked(request).mock.calls[0][0]).toContain("/batch-delete");
});
