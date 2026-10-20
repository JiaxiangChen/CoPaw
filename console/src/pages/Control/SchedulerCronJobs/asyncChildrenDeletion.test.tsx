import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { request } from "@/api/request";
import { ManagedChildrenModal } from "./ManagedChildrenModal";

vi.mock("@/api/request", () => ({ request: vi.fn() }));
const context = {
  actor_id: "owner",
  tenant_id: "owner",
  source_id: "bank",
  agent_id: "default",
};
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
const show = () =>
  render(
    <ManagedChildrenModal
      context={context}
      job={{ id: "parent", name: "父任务" } as never}
      onClose={() => {}}
      onEdit={() => {}}
      onChanged={() => {}}
    />,
  );

it("重新进入查回服务器进度，轮询完成不重新POST，关闭后停止请求", async () => {
  let done = false;
  const task = () => ({
    request_id: "request",
    operation_id: "persisted-op",
    source_job_id: "parent",
    operation_status: done ? "completed" : "running",
    status: done ? "completed" : "running",
    total_count: 2,
    deleted_count: done ? 2 : 0,
    completed_count: done ? 2 : 0,
    failed_count: 0,
    pending_count: done ? 0 : 2,
    results: [],
  });
  vi.mocked(request).mockImplementation(async (path) =>
    path.endsWith("/tasks/current")
      ? { task: task() }
      : { items: [], next_cursor: null },
  );
  const page = show();
  await screen.findByText(/后台排队或处理中/);
  expect(screen.getByRole("button", { name: "删除全部子任务" })).toBeDisabled();
  done = true;
  await screen.findByText(
    /异步删除 persisted-op：处理结束/,
    {},
    { timeout: 6000 },
  );
  expect(screen.getByText(/删除 2 \/ 完成 2/)).toHaveTextContent(
    "删除 2 / 完成 2 / 待处理 0 / 失败 0",
  );
  page.unmount();
  const count = vi.mocked(request).mock.calls.length;
  await new Promise((resolve) => setTimeout(resolve, 3100));
  expect(vi.mocked(request).mock.calls).toHaveLength(count);
  show();
  await screen.findByText(/异步删除 persisted-op：处理结束/);
  expect(
    vi
      .mocked(request)
      .mock.calls.every(
        ([, options]) => !options?.method || options.method === "GET",
      ),
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "结束本次操作" }));
  await waitFor(() =>
    expect(screen.queryByText(/异步删除 persisted-op/)).toBeNull(),
  );
}, 20000);

it("父页面刷新产生新回调不会对同一删除计数反复刷新", async () => {
  vi.mocked(request).mockImplementation(async (path) =>
    path.endsWith("/tasks/current")
      ? {
          task: {
            operation_id: "done",
            operation_status: "completed",
            status: "completed",
            total_count: 1,
            deleted_count: 1,
            completed_count: 1,
            failed_count: 0,
            pending_count: 0,
            results: [],
          },
        }
      : { items: [], next_cursor: null },
  );
  const first = vi.fn();
  const props = {
    context,
    job: { id: "parent", name: "父任务" } as never,
    onClose: () => {},
    onEdit: () => {},
  };
  const page = render(<ManagedChildrenModal {...props} onChanged={first} />);
  await waitFor(() => expect(first).toHaveBeenCalledTimes(1));
  const next = vi.fn();
  page.rerender(<ManagedChildrenModal {...props} onChanged={next} />);
  expect(next).not.toHaveBeenCalled();
});
