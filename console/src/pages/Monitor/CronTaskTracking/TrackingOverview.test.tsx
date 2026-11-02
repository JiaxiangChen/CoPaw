import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import TrackingOverview from "./TrackingOverview";

vi.mock("../../../api/modules/cronTaskTracking", () => ({
  cronTaskTracking: {
    overview: vi.fn().mockResolvedValue({
      definition: {
        name: "任务",
        run_start_date: "2026-11-01",
        run_end_date: "2026-11-30",
      },
      state: { date_window_paused: 1, date_window_sync_required: 1 },
      identity: {
        tenant_id: "alice",
        source_id: "bank",
        agent_id: "default",
        job_id: "job",
      },
      relation: "parent",
      history_only: false,
      warnings: [],
      coverage: {},
      query_id: "query",
    }),
  },
}));

it("追踪当前属性展示可执行时间区间及日期同步标记", async () => {
  render(
    <TrackingOverview
      source="bank"
      identity={{
        tenant_id: "alice",
        source_id: "bank",
        agent_id: "default",
        job_id: "job",
      }}
      revision={0}
    />,
  );
  await screen.findByText("可执行时间区间");
  expect(screen.getByText("2026-11-01 至 2026-11-30")).toBeVisible();
  expect(screen.getByText("待同步")).toBeVisible();
});
