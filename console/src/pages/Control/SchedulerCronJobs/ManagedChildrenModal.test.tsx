import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { request } from "@/api/request";
import { ManagedChildrenModal } from "./ManagedChildrenModal";
import { Modal } from "antd";
vi.mock("@/api/request", () => ({ request: vi.fn() }));
const context = {
  actor_id: "owner",
  tenant_id: "owner",
  source_id: "bank",
  agent_id: "default",
};
afterEach(() => {
  cleanup();
  Modal.destroyAll();
  vi.restoreAllMocks();
});
beforeEach(() => vi.clearAllMocks());
it("源管理者启用子任务只切换目标 Header，不冒充接收人", async () => {
  vi.mocked(request).mockImplementation(async (path) =>
    path.endsWith("/enabled")
      ? { operation: { operation_id: "op", status: "completed" } }
      : {
          items: [
            {
              id: "child",
              job_id: "child",
              name: "子任务",
              tenant_id: "receiver",
              source_id: "bank",
              agent_id: "child-agent",
              enabled: false,
              management_version: 7,
              definition_version: 3,
              schedule: { cron: "0 9 * * *" },
            },
          ],
          next_cursor: null,
        },
  );
  render(
    <ManagedChildrenModal
      context={context}
      job={{ id: "parent", name: "父任务" } as never}
      onClose={() => {}}
      onEdit={() => {}}
      onChanged={() => {}}
    />,
  );
  await screen.findByText("子任务");
  fireEvent.click(screen.getByRole("button", { name: "启用 receiver 子任务" }));
  await waitFor(() =>
    expect(
      vi.mocked(request).mock.calls.some(([path]) => path.endsWith("/enabled")),
    ).toBe(true),
  );
  const [, options] = vi
    .mocked(request)
    .mock.calls.find(([path]) => path.endsWith("/enabled"))!;
  expect(options?.headers).toMatchObject({
    "X-User-Id": "owner",
    "X-Tenant-Id": "receiver",
    "X-Agent-Id": "child-agent",
  });
  expect(JSON.parse(String(options?.body))).toMatchObject({
    expected_management_version: 7,
    enabled: true,
  });
});

it("完整分页结果支持姓名UID搜索、同名提示、资料展示及清除隐藏选择", async () => {
  const children = Array.from({ length: 12 }, (_, index) => ({
    id: `child-${index}`,
    job_id: `child-${index}`,
    name: `子任务-${index}`,
    tenant_id: index === 11 ? "NEEDLE-UID" : `receiver-${index}`,
    tenant_name: index < 2 ? "张三" : `用户-${index}`,
    bbk_id: index < 2 ? "755" : "110",
    source_id: "bank",
    agent_id: "default",
    enabled: true,
    definition_version: 1,
    management_version: 1,
    dispatch_state: index === 0 ? "batch_paused" : "ordinary",
    schedule: { type: "cron", cron: "0 9 * * *", timezone: "Asia/Shanghai" },
    state: { last_status: index ? "success" : "error" },
  }));
  vi.mocked(request).mockImplementation(async (path) =>
    path.includes("cursor=")
      ? { items: children.slice(6), next_cursor: null }
      : { items: children.slice(0, 6), next_cursor: "second-page" },
  );
  render(
    <ManagedChildrenModal
      context={context}
      job={{ id: "parent", name: "父任务" } as never}
      onClose={() => {}}
      onEdit={() => {}}
      onChanged={() => {}}
    />,
  );
  await screen.findByText("子任务-0");
  expect(
    await screen.findByText("存在同名用户，请以 UID 区分"),
  ).toBeInTheDocument();
  expect(screen.getByText("张三 (2 个 UID)")).toBeInTheDocument();
  const row = screen.getByText("子任务-0").closest("tr")!;
  expect(within(row).getByText("张三")).toBeInTheDocument();
  expect(within(row).getByText("receiver-0")).toBeInTheDocument();
  expect(within(row).getByText("755")).toBeInTheDocument();
  expect(within(row).getByText("Asia/Shanghai")).toBeInTheDocument();
  expect(within(row).getByText("error")).toBeInTheDocument();
  const schedule = within(row).getByText("0 9 * * *").parentElement!;
  expect(within(schedule).getByText("批调度")).toHaveClass(
    "ant-tag-processing",
  );
  expect(
    within(screen.getByText("子任务-1").closest("tr")!).queryByText("批调度"),
  ).toBeNull();
  fireEvent.click(within(row).getByRole("checkbox"));
  expect(screen.getByRole("button", { name: "执行选中任务" })).toBeEnabled();
  fireEvent.click(screen.getByTitle("2"));
  await screen.findByText("子任务-11");
  fireEvent.change(screen.getByLabelText("搜索子任务用户"), {
    target: { value: " needle-uid " },
  });
  fireEvent.click(screen.getByRole("button", { name: /搜\s*索/ }));
  expect(screen.getByText("子任务-11")).toBeInTheDocument();
  expect(screen.queryByText("子任务-0")).toBeNull();
  expect(screen.getByRole("button", { name: "执行选中任务" })).toBeDisabled();
  expect(screen.getByTitle("1")).toHaveClass("ant-pagination-item-active");
  expect(
    vi
      .mocked(request)
      .mock.calls.filter(([path]) => !path.includes("/tasks/current")),
  ).toHaveLength(2);
  const matchedRow = screen.getByText("子任务-11").closest("tr")!;
  fireEvent.click(within(matchedRow).getByRole("checkbox"));
  expect(screen.getByRole("button", { name: "执行选中任务" })).toBeEnabled();
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "筛选子任务机构" }));
  const option = (await screen.findAllByText("755")).find((element) =>
    element.closest(".ant-select-item-option"),
  );
  expect(option).toBeDefined();
  fireEvent.click(option!);
  expect(screen.queryByText("子任务-11")).toBeNull();
  expect(screen.getByRole("button", { name: "执行选中任务" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("搜索子任务用户"), {
    target: { value: "张三" },
  });
  fireEvent.click(screen.getByRole("button", { name: /搜\s*索/ }));
  expect(screen.getByText("子任务-0")).toBeInTheDocument();
  expect(screen.getByText("子任务-1")).toBeInTheDocument();
  expect(
    vi
      .mocked(request)
      .mock.calls.filter(([path]) => !path.includes("/tasks/current")),
  ).toHaveLength(2);
}, 20000);

it("删除全部覆盖跨页且不受搜索影响，取消不删除，确认保留父任务身份", async () => {
  const confirm = Modal.confirm;
  vi.spyOn(Modal, "confirm").mockImplementation((options) =>
    confirm({ ...options, transitionName: "", maskTransitionName: "" }),
  );
  const children = Array.from({ length: 12 }, (_, index) => ({
    id: `all-${index}`,
    name: `全部子任务-${index}`,
    tenant_id: `UID-${index}`,
    source_id: "bank",
    agent_id: "default",
    enabled: true,
    definition_version: index + 1,
    management_version: 1,
    deleted: index === 11,
    schedule: { cron: "0 9 * * *" },
  }));
  const changed = vi.fn();
  vi.mocked(request).mockImplementation(async (path, options) => {
    if (path.endsWith("/tasks/current")) return { task: null };
    if (path.endsWith("/delete-all")) {
      const body = JSON.parse(String(options?.body));
      return {
        request_id: body.request_id,
        operation_id: "delete-all-op",
        operation_status: "completed",
        source_job_id: "parent",
        status: "completed",
        total_count: body.items.length,
        deleted_count: body.items.length,
        completed_count: body.items.length,
        pending_count: 0,
        failed_count: 0,
        results: body.items.map(
          (item: { job_id: string; tenant_id: string; agent_id: string }) => ({
            ...item,
            source_id: "bank",
            status: "completed",
            http_status: 200,
            logical_delete_applied: true,
          }),
        ),
      };
    }
    return path.includes("cursor=")
      ? { items: children.slice(6), next_cursor: null }
      : { items: children.slice(0, 6), next_cursor: "more" };
  });
  render(
    <ManagedChildrenModal
      context={context}
      job={{ id: "parent", name: "父任务" } as never}
      onClose={() => {}}
      onEdit={() => {}}
      onChanged={changed}
    />,
  );
  await screen.findByText("全部子任务-0");
  fireEvent.change(screen.getByLabelText("搜索子任务用户"), {
    target: { value: "UID-0" },
  });
  fireEvent.click(screen.getByRole("button", { name: /搜\s*索/ }));
  expect(screen.queryByText("全部子任务-1")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "删除全部子任务" }));
  // rc-util 在测试环境为两个 Modal 使用相同 ID，按实际标题定位确认框。
  const confirmation = (
    await screen.findAllByText("删除全部 11 个子任务？")
  )[0].closest<HTMLElement>('[role="dialog"]')!;
  expect(
    within(confirmation).getByText(/不受搜索、机构筛选或分页影响/),
  ).toBeVisible();
  fireEvent.click(
    within(confirmation).getByRole("button", { name: /取\s*消/ }),
  );
  await waitFor(() =>
    expect(screen.queryAllByText("删除全部 11 个子任务？")).toHaveLength(0),
  );
  expect(
    vi
      .mocked(request)
      .mock.calls.filter(([path]) => path.endsWith("/delete-all")),
  ).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "删除全部子任务" }));
  const approved = (
    await screen.findAllByText("删除全部 11 个子任务？")
  )[0].closest<HTMLElement>('[role="dialog"]')!;
  fireEvent.click(
    within(approved).getByRole("button", { name: "确认删除全部" }),
  );
  await waitFor(() => expect(changed).toHaveBeenCalled());
  const calls = vi
    .mocked(request)
    .mock.calls.filter(([path]) => path.endsWith("/delete-all"));
  expect(calls).toHaveLength(1);
  const [path, options] = calls[0];
  expect(path).toBe(
    "/scheduler/cron/jobs/parent/broadcast/children/delete-all",
  );
  const body = JSON.parse(String(options?.body));
  expect(body.items).toHaveLength(11);
  expect(body.items.map((item: { job_id: string }) => item.job_id)).toEqual(
    children.slice(0, 11).map((child) => child.id),
  );
  expect(body.items[10]).toMatchObject({
    tenant_id: "UID-10",
    expected_definition_version: 11,
  });
  expect(options?.headers).toMatchObject({
    "X-User-Id": "owner",
    "X-Tenant-Id": "owner",
    "X-Source-Id": "bank",
  });
}, 20000);

it("读取全量子任务失败不发删除请求，空列表禁用删除全部", async () => {
  vi.mocked(request).mockResolvedValue({ items: [], next_cursor: null });
  const view = render(
    <ManagedChildrenModal
      context={context}
      job={{ id: "parent", name: "父任务" } as never}
      onClose={() => {}}
      onEdit={() => {}}
      onChanged={() => {}}
    />,
  );
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "删除全部子任务" }),
    ).toBeDisabled(),
  );
  view.unmount();
  vi.mocked(request).mockResolvedValueOnce({
    items: [
      {
        id: "child",
        name: "可删除任务",
        tenant_id: "bob",
        agent_id: "default",
        definition_version: 1,
      },
    ],
    next_cursor: null,
  });
  vi.mocked(request).mockRejectedValue(new Error("全量清单读取失败"));
  render(
    <ManagedChildrenModal
      context={context}
      job={{ id: "parent", name: "父任务" } as never}
      onClose={() => {}}
      onEdit={() => {}}
      onChanged={() => {}}
    />,
  );
  await screen.findByText("可删除任务");
  fireEvent.click(screen.getByRole("button", { name: "删除全部子任务" }));
  await screen.findByText("全量清单读取失败");
  expect(screen.queryByRole("button", { name: "确认删除全部" })).toBeNull();
  expect(
    vi
      .mocked(request)
      .mock.calls.filter(([path]) => path.endsWith("/batch-delete")),
  ).toHaveLength(0);
}, 20000);
