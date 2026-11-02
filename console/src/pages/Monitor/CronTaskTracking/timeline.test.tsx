import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import TrackingTimeline from "./TrackingTimeline";
import type {
  TaskIdentity,
  TrackingTimeline as TimelineResponse,
} from "../../../api/types/cronTaskTracking";

const api = vi.hoisted(() => ({ timeline: vi.fn() }));
vi.mock("../../../api/modules/cronTaskTracking", () => ({
  cronTaskTracking: api,
}));
afterEach(cleanup);
const identity: TaskIdentity = {
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
  job_id: "job-1",
};
const response = (title: string): TimelineResponse => ({
  identity,
  items: [
    {
      event_id: title,
      node_type: "E09",
      title,
      category: "execution",
      occurred_at: "2026-10-27T00:00:00Z",
      time_basis: "scheduler_confirmed",
      entry: "batch",
      recorded_outcome: "error",
      details: {},
      source_refs: [],
      gaps: [],
    },
  ],
  coverage: {},
  warnings: [],
  query_id: "query",
  queried_at: "2026-10-27T08:00:00+08:00",
  loaded_count: 1,
  has_more: false,
  next_cursor: null,
  query_upper: "2026-10-28T00:00:00+08:00",
  consistency: "best_effort_current_records",
});

it("ignores a late timeline from an older identity after switching tasks", async () => {
  let finish: (value: TimelineResponse) => void = () => undefined;
  api.timeline
    .mockImplementationOnce(
      () =>
        new Promise<TimelineResponse>((resolve) => {
          finish = resolve;
        }),
    )
    .mockResolvedValueOnce(response("新任务记录"));
  const { rerender } = render(
    <TrackingTimeline
      source="bank"
      identity={identity}
      filters={{ category: "all" }}
    />,
  );
  rerender(
    <TrackingTimeline
      source="bank"
      identity={{ ...identity, job_id: "job-2" }}
      filters={{ category: "all" }}
    />,
  );
  expect(await screen.findByText("新任务记录")).toBeTruthy();
  await act(async () => finish(response("旧任务迟到记录")));
  expect(screen.queryByText("旧任务迟到记录")).toBeNull();
});

it("renders an offset-aware UTC fact as Beijing time without treating confirmation as actual completion", async () => {
  api.timeline.mockResolvedValueOnce(response("Scheduler 已确认整轮结果"));
  render(<TrackingTimeline source="bank" identity={identity} filters={{}} />);
  expect(await screen.findByText("2026-10-27 08:00:00")).toBeTruthy();
  expect(screen.getByText("确认时间，非异步实际完成时间")).toBeTruthy();
  expect(screen.queryByText("异步任务实际完成")).toBeNull();
});
