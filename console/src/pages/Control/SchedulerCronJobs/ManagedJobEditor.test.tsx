import {
  cleanup,
  render,
  screen,
  waitFor,
  fireEvent,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { request } from "@/api/request";
import { ManagedJobEditor } from "./ManagedJobEditor";

vi.mock("@/api/request", () => ({ request: vi.fn() }));
vi.mock("@agentscope-ai/design", async (original) => ({
  ...(await original<object>()),
  Drawer: (await import("antd")).Drawer,
}));
vi.mock("@/api", () => ({
  default: {
    listSweSkills: vi.fn().mockResolvedValue({ skills: [] }),
    getUserTimezone: vi.fn().mockResolvedValue({ timezone: "Asia/Shanghai" }),
  },
}));
vi.mock("@/hooks/useExecutionModelOptions", async (original) => ({
  ...(await original<object>()),
  useExecutionModelOptions: () => ({
    options: [],
    loading: false,
    tenantDefaultLabel: "默认模型",
  }),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
const context = {
  actor_id: "owner",
  tenant_id: "owner",
  source_id: "bank",
  agent_id: "default",
};
const job = {
  id: "time-job",
  name: "十点任务",
  enabled: true,
  schedule: { type: "cron", cron: "0 10 * * *", timezone: "Asia/Shanghai" },
  task_type: "text",
  text: "test",
  dispatch: {
    type: "channel",
    channel: "console",
    target: { user_id: "owner", session_id: "cron-session" },
    mode: "final",
  },
  runtime: {
    max_concurrency: 1,
    timeout_seconds: 7200,
    misfire_grace_seconds: 300,
  },
  meta: {},
  management_owner: "scheduler",
  tenant_id: "owner",
  source_id: "bank",
  agent_id: "default",
  definition_version: 2,
  management_version: 2,
  state_version: 2,
  management_status: "ready",
  deleted: false,
  dispatch_state: "ordinary",
} as const;

it("列表10点的任务打开真实编辑表单应为10点，原样保存仍为10点", async () => {
  const { schedule, ...rest } = job;
  vi.mocked(request).mockResolvedValue({ ...rest, spec: { ...job, schedule } });
  const save = vi
    .fn()
    .mockResolvedValue({ operation_id: "save", status: "completed" });
  render(
    <ManagedJobEditor
      context={context}
      job={job}
      saving={false}
      onSave={save}
      onClose={() => {}}
    />,
  );
  await screen.findByDisplayValue("十点任务");
  await waitFor(() =>
    expect(screen.getByDisplayValue("10:00")).toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(save).toHaveBeenCalled());
  expect(save.mock.calls[0][1].schedule).toMatchObject({
    cron: "0 10 * * *",
    timezone: "Asia/Shanghai",
  });
});

it("低于列表定义版本的详情不允许回填旧时间并提交", async () => {
  vi.mocked(request).mockResolvedValue({
    ...job,
    definition_version: 1,
    spec: { ...job, schedule: { ...job.schedule, cron: "0 9 * * *" } },
  });
  const save = vi.fn();
  render(
    <ManagedJobEditor
      context={context}
      job={job}
      saving={false}
      onSave={save}
      onClose={() => {}}
    />,
  );
  await screen.findByText("任务详情版本落后于列表，请刷新后重新编辑。");
  expect(screen.getByRole("button", { name: /保\s*存/ })).toBeDisabled();
  expect(save).not.toHaveBeenCalled();
});

it.each(["0 9,10,11 * * *", "*/5 * * * *", "0 10 15 * *"])(
  "真实编辑框以自定义模式展示%s并保留原表达式",
  async (cron) => {
    const { schedule, ...status } = job;
    vi.mocked(request).mockResolvedValue({
      ...status,
      spec: { ...job, schedule: { ...schedule, cron } },
    });
    const save = vi.fn().mockResolvedValue({ operation_id: "save" });
    render(
      <ManagedJobEditor
        context={context}
        job={job}
        saving={false}
        onSave={save}
        onClose={() => {}}
      />,
    );
    expect(
      await screen.findByLabelText("cronJobs.cronCustomExpression"),
    ).toHaveValue(cron);
    expect(screen.queryByDisplayValue("09:00")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save.mock.calls[0][1].schedule.cron).toBe(cron);
  },
);
