import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import BatchDetail from "./BatchDetail";
import type { ManagedBatch } from "@/api/modules/managedBatchBoard";

const api = vi.hoisted(() => ({ detail: vi.fn() }));
vi.mock("../../../api/modules/managedBatchBoard", () => ({
  managedBatchBoard: api,
}));
vi.mock("./FailurePanel", () => ({ default: () => null }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
it("详情不展示任务数说明行，成员保留子任务状态文案", async () => {
  const batch = {
    batch_id: "batch",
    job_id: "parent",
    job_name: "日报",
    member_count: 1,
    status: "running",
  } as ManagedBatch;
  api.detail.mockResolvedValue({
    batch,
    members: [
      {
        member_id: "member",
        member_role: "parent",
        member_order: 0,
        job_id: "parent",
        job_name: "日报任务",
        tenant_id: "owner",
        execution_id: "exec",
        business_attempt: 1,
        dispatch_attempt: 1,
        status: "success",
        settled_status: "success",
        dispatch_priority: {},
        definition_version: 1,
        current_definition_version: 1,
      },
    ],
    member_total: 1,
    events: [],
    event_total: 0,
  });
  render(
    <BatchDetail
      source="bank"
      actor="manager"
      batch={batch}
      refresh={0}
      onQueued={() => {}}
    />,
  );
  await screen.findByText("日报任务");
  expect(screen.queryByText(/主执行状态与异步收敛状态分别展示/)).toBeNull();
  expect(screen.queryByText(/任务数.*含父任务/)).toBeNull();
  expect(screen.getByRole("tab", { name: "执行成员 (1)" })).toBeInTheDocument();
  expect(screen.getByText("子任务状态：success")).toBeInTheDocument();
  expect(screen.queryByText(/收敛/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /expand|展开/i }));
  expect(screen.queryByText(/；收敛/)).toBeNull();
  expect(screen.getByText(/子任务完成/)).toBeInTheDocument();
});
