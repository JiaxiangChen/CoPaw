import { expect, it, vi } from "vitest";
import { cronTaskTracking } from "./cronTaskTracking";

const call = vi.hoisted(() => vi.fn().mockResolvedValue({}));
vi.mock("../request", () => ({ request: call }));

it("uses only read-only Scheduler routes and trusted source headers", async () => {
  const controller = new AbortController();
  await cronTaskTracking.lookup("bank", "id/a", {}, controller.signal);
  expect(call).toHaveBeenLastCalledWith(
    "/scheduler/cron/task-tracking/jobs/id%2Fa/lookup",
    { headers: { "X-Source-Id": "bank" }, signal: controller.signal },
  );
  await cronTaskTracking.timeline(
    "bank",
    {
      tenant_id: "alice",
      source_id: "bank",
      agent_id: "default",
      job_id: "job-1",
    },
    { category: "execution", limit: 50 },
  );
  const [url, options] = call.mock.calls[call.mock.calls.length - 1];
  expect(url).toContain("/job-1/timeline?");
  expect(url).toContain("tenant_id=alice");
  expect(url).toContain("category=execution");
  expect(url).not.toContain("source_id=");
  expect(options.method).toBeUndefined();
  expect(options.body).toBeUndefined();
});
