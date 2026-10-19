import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { TFunction } from "i18next";
import type { ColumnType } from "antd/es/table";
import type {
  ManagedDispatchMode,
  ManagedJob,
} from "@/api/types/schedulerCron";
import { createManagedColumns } from "./columns";

afterEach(cleanup);
const job = (mode: ManagedDispatchMode, child: boolean): ManagedJob =>
  ({
    id: "new-job",
    name: "任务",
    tenant_id: "alice",
    source_id: "bank",
    agent_id: "default",
    dispatch_state: mode,
    schedule: {
      type: "cron",
      cron: child ? "30 10 * * *" : "0 9 * * *",
      timezone: "Asia/Shanghai",
    },
    meta: child ? { broadcast_source_job_id: "parent" } : {},
  }) as ManagedJob;

function column(key: string): ColumnType<ManagedJob> {
  return createManagedColumns({
    t: ((key: string) => key) as TFunction,
    models: { options: [], tenantDefaultLabel: "默认模型" },
    message: { success: vi.fn(), error: vi.fn() },
    perform: vi.fn(),
    setPanel: vi.fn(),
    busy: false,
  }).find((item) => item.key === key)! as ColumnType<ManagedJob>;
}

it.each(["batch_enabled", "batch_paused"] as const)(
  "%s 的父任务和子任务都在自己的执行时间前显示批调度",
  (mode) => {
    for (const child of [false, true]) {
      const record = job(mode, child);
      const view = render(
        <>{column("cron").render?.(record.schedule.cron, record, 0)}</>,
      );
      const badge = screen.getByText("批调度");
      expect(badge).toHaveClass("ant-tag-processing");
      expect(badge.nextElementSibling).toHaveTextContent(
        child ? "10:30" : "09:00",
      );
      expect(view.container.textContent).not.toContain("05:00");
      view.unmount();
    }
  },
);

it("普通任务即使残留旧批标记也不显示，普通子任务保留自己的时间", () => {
  const record = job("ordinary", true);
  record.meta!.broadcast_dispatch_intents_enabled = true;
  render(<>{column("cron").render?.(record.schedule.cron, record, 0)}</>);
  expect(screen.queryByText("批调度")).toBeNull();
  expect(screen.getByText(/10:30/)).toBeVisible();
});

it("增加执行时间标识不移除任务名旁的分发子任务标签", () => {
  const record = job("batch_enabled", true);
  render(<>{column("name").render?.(record.name, record, 0)}</>);
  expect(screen.getByText("任务")).toBeVisible();
  expect(screen.getByText("分发子任务")).toBeVisible();
});

it.each(["技能任务执行失败 [workflow=http_status, HTTP=502]", ""])(
  "新版error悬浮展示当前失败详情或明确缺失提示：%s",
  async (detail) => {
    const record = {
      ...job("ordinary", false),
      state: { last_status: "error", last_error: detail },
    } as ManagedJob;
    render(<>{column("last_status").render?.(null, record, 0)}</>);
    fireEvent.mouseEnter(screen.getByText("error"));
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      detail || "暂无失败详情，请查看执行日志",
    );
  },
);

it.each([false, true])(
  "成功或正在运行时不展示旧错误，运行优先=%s",
  (running) => {
    const record = {
      ...job("ordinary", false),
      state: {
        last_status: running ? "error" : "success",
        last_error: "过期错误",
      },
      task: { is_running: running },
    } as ManagedJob;
    render(<>{column("last_status").render?.(null, record, 0)}</>);
    fireEvent.mouseEnter(screen.getByText(running ? "running" : "success"));
    expect(screen.queryByRole("tooltip")).toBeNull();
  },
);
