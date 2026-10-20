import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import { ManagedBroadcastModal } from "./ManagedBroadcastModal";
import type { ManagedJob } from "@/api/types/schedulerCron";

vi.mock("@/components/TenantSelector", () => ({
  TenantSelector: () => <div>接收人</div>,
}));
vi.mock("@/api/modules/schedulerCron", () => ({
  schedulerCronApi: {
    currentBroadcast: vi.fn(),
    broadcast: vi.fn(),
    broadcastTask: vi.fn(),
  },
}));
const context = {
  actor_id: "u",
  tenant_id: "u",
  source_id: "s",
  agent_id: "a",
};
const job = { id: "j", name: "任务", definition_version: 1 } as ManagedJob;
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(schedulerCronApi.currentBroadcast).mockResolvedValue({
    task: null,
  });
});

it("刷新旧轮进度期间不能开启新轮，避免迟到结果覆盖新轮", async () => {
  const task = {
    task_id: "finished",
    status: "succeeded",
    results: [],
  } as never;
  vi.mocked(schedulerCronApi.currentBroadcast).mockResolvedValue({ task });
  vi.mocked(schedulerCronApi.broadcastTask).mockResolvedValue(task);
  render(
    <ManagedBroadcastModal context={context} job={job} onClose={() => {}} />,
  );
  const nextRound = await screen.findByRole("button", { name: "新一轮分发" });
  const refresh = screen.getByRole("button", { name: /刷新进度/ });
  await waitFor(() => expect(nextRound).not.toBeDisabled());
  let resolve!: (value: typeof task) => void;
  vi.mocked(schedulerCronApi.broadcastTask).mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  fireEvent.click(refresh);
  await waitFor(() => expect(nextRound).toBeDisabled());
  await act(async () => {
    resolve(task);
  });
  await waitFor(() => expect(nextRound).not.toBeDisabled());
  expect(nextRound).toBeInTheDocument();
});

it("零人分发只提交散列配置，不携带调度模式", async () => {
  vi.mocked(schedulerCronApi.broadcast).mockResolvedValue({
    task_id: "task",
    status: "queued",
    results: [],
  } as never);
  render(
    <ManagedBroadcastModal context={context} job={job} onClose={() => {}} />,
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "开始分发" })).not.toBeDisabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "开始分发" }));
  await waitFor(() => expect(schedulerCronApi.broadcast).toHaveBeenCalled());
  const body = vi.mocked(schedulerCronApi.broadcast).mock.calls[0][2];
  expect(body).toMatchObject({
    targets: [],
    expected_definition_version: 1,
    enable_offset: true,
    offset_window_hours: 4,
  });
  expect(body).not.toHaveProperty("state");
  expect(body).not.toHaveProperty("enable_batch_dispatch");
});

it("请求失败重发原 request_id，不自动创建新一轮", async () => {
  vi.mocked(schedulerCronApi.broadcast).mockRejectedValue(
    new Error("网络中断"),
  );
  render(
    <ManagedBroadcastModal context={context} job={job} onClose={() => {}} />,
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "开始分发" })).not.toBeDisabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "开始分发" }));
  await screen.findByText("网络中断");
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /开始分发/ })).not.toBeDisabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: /开始分发/ }));
  await waitFor(() =>
    expect(schedulerCronApi.broadcast).toHaveBeenCalledTimes(2),
  );
  expect(
    vi.mocked(schedulerCronApi.broadcast).mock.calls[0][2].request_id,
  ).toBe(vi.mocked(schedulerCronApi.broadcast).mock.calls[1][2].request_id);
});
