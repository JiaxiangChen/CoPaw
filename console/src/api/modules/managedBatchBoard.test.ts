import { beforeEach, describe, expect, it, vi } from "vitest";
import { managedBatchBoard, retryCandidates } from "./managedBatchBoard";

const mocked = vi.hoisted(() => vi.fn().mockResolvedValue({}));
vi.mock("../request", () => ({ request: mocked }));
describe("新版看板独立契约", () => {
  beforeEach(() => mocked.mockClear());
  it("查询和写入只走新服务路径，来源放在 Header", async () => {
    await managedBatchBoard.batches("bank", { query: "a%", page: 2 });
    expect(mocked).toHaveBeenLastCalledWith(
      "/monitor/cron/managed-dispatch/batches?query=a%25&page=2",
      expect.objectContaining({ headers: { "X-Source-Id": "bank" } }),
    );
    await managedBatchBoard.failures("bank", "id/a", [
      "other",
      "execution_timeout",
    ]);
    expect(mocked.mock.calls[1][0]).toBe(
      "/scheduler/cron/managed-dispatch/batches/id%2Fa/failures?types=other&types=execution_timeout",
    );
    const body = {
      request_id: "stable",
      candidates: [],
      confirm_stopped: true,
    };
    await managedBatchBoard.retry("bank", "batch", body);
    expect(mocked).toHaveBeenLastCalledWith(
      "/scheduler/cron/managed-dispatch/batches/batch/retry",
      expect.objectContaining({ method: "POST", body: JSON.stringify(body) }),
    );
  });
  it("提交只投递六个候选版本字段，不回传用户、角色或正文", () => {
    const item = {
      member_id: "m",
      execution_id: "uuid",
      member_version: 1,
      execution_version: 2,
      definition_version: 3,
      business_attempt: 1,
      tenant_id: "alice",
      job_id: "job",
      failure_type: "other",
      status: "error",
      transport_only: false,
      execution_definition_version: 1,
    };
    expect(retryCandidates([item])).toEqual([
      {
        member_id: "m",
        execution_id: "uuid",
        member_version: 1,
        execution_version: 2,
        definition_version: 3,
        business_attempt: 1,
      },
    ]);
  });
});
