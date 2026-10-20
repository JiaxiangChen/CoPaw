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
import { useIframeStore } from "@/stores/iframeStore";
import SchedulerCronJobsPage from "./index";
import type { ManagedJob } from "@/api/types/schedulerCron";
import { Modal } from "antd";

vi.mock("@/api/request", () => ({ request: vi.fn() }));
vi.mock("@agentscope-ai/design", async (original) => {
  const [design, antd] = await Promise.all([
    original<object>(),
    import("antd"),
  ]);
  return { ...design, Dropdown: antd.Dropdown, Drawer: antd.Drawer };
});
vi.mock("@/api", () => ({
  default: {
    listSweSkills: vi.fn().mockResolvedValue({ skills: [] }),
    getUserTimezone: vi.fn().mockResolvedValue({ timezone: "Asia/Shanghai" }),
  },
}));
vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: { success: vi.fn(), error: vi.fn() } }),
}));
vi.mock("@/hooks/useExecutionModelOptions", async (original) => ({
  ...(await original<object>()),
  useExecutionModelOptions: () => ({
    options: [],
    loading: false,
    tenantDefaultLabel: "默认模型",
  }),
}));
vi.mock("@/components/PageHeader", () => ({
  PageHeader: ({ extra }: { extra: React.ReactNode }) => (
    <header>定时任务（新）{extra}</header>
  ),
}));
const job = {
  id: "new-job",
  name: "新版任务",
  enabled: false,
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
  definition_version: 3,
  management_version: 5,
  management_status: "ready",
  management_owner: "scheduler",
  dispatch_state: "ordinary",
  deleted: false,
  schedule: { type: "cron", cron: "0 9 * * *", timezone: "Asia/Shanghai" },
  task_type: "text",
  text: "测试",
  dispatch: {
    type: "channel",
    channel: "console",
    target: { user_id: "alice", session_id: "session" },
  },
} as ManagedJob;
const operation = {
  operation_id: "op",
  job_id: job.id,
  action: "enable",
  status: "completed",
  stage: "completed",
  version: 1,
  expected_version: 5,
  logical_delete_applied: false,
};
afterEach(() => {
  cleanup();
  Modal.destroyAll();
  vi.restoreAllMocks();
});
beforeEach(() => {
  vi.clearAllMocks();
  useIframeStore.setState({
    userId: "alice",
    source: "bank",
    manager: false,
    isSuperManager: false,
  });
  vi.mocked(request).mockImplementation(async (url) => {
    if (url.includes("/operations/")) return operation;
    if (url.includes("/enabled")) return { operation };
    return { items: [job], next_cursor: null };
  });
});

it("编辑读取最新详情后同步列表排程，不保留旧列表10点而表单9点", async () => {
  const listed = { ...job, schedule: { ...job.schedule, cron: "0 10 * * *" } };
  vi.mocked(request).mockImplementation(async (url) => {
    if (url.includes("/new-job?"))
      return {
        ...job,
        spec: { ...job, schedule: { ...job.schedule, cron: "0 9 * * *" } },
      };
    return { items: [listed], next_cursor: null };
  });
  render(<SchedulerCronJobsPage />);
  await screen.findByText(/ 10:00$/);
  fireEvent.click(screen.getByRole("button", { name: "更多操作 新版任务" }));
  fireEvent.click(await screen.findByText("编辑", { exact: true }));
  await screen.findByDisplayValue("09:00");
  await waitFor(() => expect(screen.queryByText(/ 10:00$/)).toBeNull());
  expect(screen.getByText(/ 09:00$/)).toBeInTheDocument();
});

it("已删除任务筛选位于页头操作区且可以开启和关闭", async () => {
  render(<SchedulerCronJobsPage />);
  await screen.findByText("新版任务");
  const header = screen.getByRole("banner");
  const filter = within(header).getByRole("button", {
    name: "包含已删除任务（查看清理进度）",
  });
  expect(within(header).getByRole("button", { name: /刷\s*新/ })).toBeVisible();
  expect(
    within(header).getByRole("button", { name: /cronJobs.createJob|创建任务/ }),
  ).toBeVisible();
  expect(filter).toHaveAttribute("aria-pressed", "false");
  filter.focus();
  expect(filter).toHaveFocus();
  fireEvent.click(filter);
  await waitFor(() =>
    expect(vi.mocked(request).mock.lastCall?.[0]).toContain(
      "include_deleted=true",
    ),
  );
  expect(filter).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(filter);
  await waitFor(() =>
    expect(vi.mocked(request).mock.lastCall?.[0]).toContain(
      "include_deleted=false",
    ),
  );
  expect(filter).toHaveAttribute("aria-pressed", "false");
});

it("新页完整列表和启用动作只走 Scheduler，携带管理版本和真实用户 Header", async () => {
  render(<SchedulerCronJobsPage />);
  await screen.findByText("新版任务");
  expect(screen.getByText("普通调度")).toBeInTheDocument();
  expect(screen.queryByText("批调度")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "启用" }));
  await waitFor(() =>
    expect(
      vi.mocked(request).mock.calls.some(([url]) => url.endsWith("/enabled")),
    ).toBe(true),
  );
  const [, options] = vi
    .mocked(request)
    .mock.calls.find(([url]) => url.endsWith("/enabled"))!;
  expect(JSON.parse(String(options?.body))).toMatchObject({
    expected_management_version: 5,
    enabled: true,
  });
  expect(options?.headers).toMatchObject({
    "X-User-Id": "alice",
    "X-Tenant-Id": "alice",
    "X-Agent-Id": "default",
    "X-Source-Id": "bank",
  });
  expect(
    vi
      .mocked(request)
      .mock.calls.every(([url]) => url.startsWith("/scheduler/cron/jobs")),
  ).toBe(true);
});

it("已停用任务不能直接执行，但可以单独编辑、分发及查看三态", async () => {
  render(<SchedulerCronJobsPage />);
  await screen.findByText("新版任务");
  expect(screen.getByRole("button", { name: "立即执行" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "更多操作 新版任务" }));
  await screen.findByText("分发");
  expect(screen.getByText("调度方式 / 批调度配置")).toBeInTheDocument();
  expect(screen.getByText("编辑")).toBeInTheDocument();
});

it("立即执行需确认，取消不发请求，确认仍按新版版本提交", async () => {
  const confirm = Modal.confirm;
  // jsdom 不执行 CSS 动画；保留真实确认框交互，仅关闭退出动画。
  vi.spyOn(Modal, "confirm").mockImplementation((options) =>
    confirm({ ...options, transitionName: "", maskTransitionName: "" }),
  );
  vi.mocked(request).mockImplementation(async (url) => {
    if (url.endsWith("/run"))
      return {
        execution: { execution_id: "exec", job_id: job.id, status: "queued" },
        reused: false,
      };
    if (url.includes("/executions/"))
      return {
        execution_id: "exec",
        job_id: job.id,
        status: "success",
        settled_status: "success",
      };
    if (url.endsWith("/operations/pending")) return { operation: null };
    return { items: [{ ...job, enabled: true }], next_cursor: null };
  });
  render(<SchedulerCronJobsPage />);
  await screen.findByText("新版任务");
  fireEvent.click(screen.getByRole("button", { name: "立即执行" }));
  expect(
    vi.mocked(request).mock.calls.some(([url]) => url.endsWith("/run")),
  ).toBe(false);
  const confirmation = await screen.findByRole("dialog", {
    name: /立即执行任务|cronJobs.executeNowTitle/,
  });
  fireEvent.click(
    within(confirmation).getByRole("button", {
      name: /取消|cronJobs.cancelText/,
    }),
  );
  await waitFor(
    () =>
      expect(
        screen.queryByRole("dialog", {
          name: /立即执行任务|cronJobs.executeNowTitle/,
        }),
      ).toBeNull(),
    { timeout: 4000 },
  );
  expect(
    vi.mocked(request).mock.calls.some(([url]) => url.endsWith("/run")),
  ).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "立即执行" }));
  const next = await screen.findByRole("dialog", {
    name: /立即执行任务|cronJobs.executeNowTitle/,
  });
  fireEvent.click(
    within(next).getByRole("button", {
      name: /^立即执行$|cronJobs.executeNowConfirm/,
    }),
  );
  await waitFor(() =>
    expect(
      vi.mocked(request).mock.calls.filter(([url]) => url.endsWith("/run")),
    ).toHaveLength(1),
  );
  const [path, options] = vi
    .mocked(request)
    .mock.calls.find(([url]) => url.endsWith("/run"))!;
  expect(path).toBe("/scheduler/cron/jobs/new-job/run");
  expect(JSON.parse(String(options?.body))).toMatchObject({
    expected_definition_version: 3,
  });
}, 20000);
