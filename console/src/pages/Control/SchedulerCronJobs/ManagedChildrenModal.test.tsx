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
vi.mock("@/api/request", () => ({ request: vi.fn() }));
const context = {
  actor_id: "owner",
  tenant_id: "owner",
  source_id: "bank",
  agent_id: "default",
};
afterEach(cleanup);
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
  expect(vi.mocked(request).mock.calls).toHaveLength(2);
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
  expect(vi.mocked(request).mock.calls).toHaveLength(2);
}, 20000);
