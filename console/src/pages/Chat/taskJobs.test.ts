import { describe, expect, it } from "vitest";
import type { CronJobSpecOutput } from "../../api/types";
import { tagTask } from "@/api/modules/cronTaskSources";
import {
  getTaskOpenTarget,
  getTaskNextRunTooltipText,
  getTaskNextRunTooltipTimes,
  getTaskPauseStatusText,
  getTaskSidebarMeta,
  partitionTasksByPauseState,
  uniqueTaskForId,
  getTaskLastRunDisplay,
  hasTaskRunRecord,
} from "./taskJobs";

it("新版手动历史使用最近运行时间和真实状态，不再被视为未启动", () => {
  const context = {
    actor_id: "user-1",
    tenant_id: "user-1",
    source_id: "bank",
    agent_id: "default",
  };
  const task = tagTask(
    taskJob({
      state: { last_run_at: "2026-10-19 05:18:00", last_status: "success" },
    }),
    "scheduler",
    context,
  );
  expect(hasTaskRunRecord(task)).toBe(true);
  expect(getTaskLastRunDisplay(task)).toEqual({
    at: "2026-10-19T05:18:00Z",
    statusText: "已完成",
  });
  task.state = { last_run_at: "2026-10-19T06:18:00Z", last_status: "error" };
  expect(getTaskLastRunDisplay(task)).toEqual({
    at: "2026-10-19T06:18:00Z",
    statusText: "执行失败",
  });
});

it("新版最新运行不被旧自动成功时间覆盖，旧任务展示语义保持", () => {
  const original = taskJob({
    state: { last_run_at: "2026-10-19T06:18:00Z", last_status: "running" },
  });
  original.task!.last_scheduled_run_at = "2026-10-18T01:00:00Z";
  const managed = tagTask(original, "scheduler", {
    actor_id: "u",
    tenant_id: "u",
    source_id: "s",
    agent_id: "a",
  });
  expect(getTaskLastRunDisplay(managed)).toEqual({
    at: "2026-10-19T06:18:00Z",
    statusText: "运行中",
  });
  expect(getTaskLastRunDisplay(original)).toEqual({
    at: "2026-10-18T01:00:00Z",
    statusText: "已完成",
  });
  expect(hasTaskRunRecord(taskJob())).toBe(false);
});

function taskJob(
  overrides: Partial<CronJobSpecOutput> = {},
): CronJobSpecOutput {
  return {
    id: "job-1",
    name: "定时任务",
    enabled: true,
    schedule: {
      type: "cron",
      cron: "0 9 * * *",
      timezone: "Asia/Shanghai",
    },
    task_type: "agent",
    request: {
      input: [{ role: "user", content: "ping" }],
    },
    dispatch: {
      type: "channel",
      channel: "console",
      target: {
        user_id: "user-1",
        session_id: "session-1",
      },
    },
    task: {
      visible_in_my_tasks: true,
      has_scheduled_result: false,
      latest_scheduled_preview: "",
      unread_execution_count: 0,
      is_running: false,
      is_paused: false,
      pause_reason: null,
    },
    ...overrides,
  };
}

describe("getTaskSidebarMeta", () => {
  it("裸任务链接遇到新旧同 ID 不静默选择旧任务", () => {
    const context = {
      actor_id: "u",
      tenant_id: "u",
      source_id: "s",
      agent_id: "a",
    };
    const oldTask = tagTask(taskJob(), "legacy", context);
    const newTask = tagTask(taskJob(), "scheduler", context);
    expect(uniqueTaskForId([oldTask, newTask], "job-1")).toBeNull();
    expect(uniqueTaskForId([newTask], "job-1")).toBe(newTask);
  });
  it("新版已启用任务允许独立编辑删除，停用任务不可执行，旧任务规则不变", () => {
    const context = {
      actor_id: "u",
      tenant_id: "u",
      source_id: "s",
      agent_id: "a",
    };
    const job = tagTask(
      { ...taskJob(), management_status: "ready" } as never,
      "scheduler",
      context,
    );
    expect(getTaskSidebarMeta(job)).toMatchObject({
      canEdit: true,
      canDelete: true,
      canRun: true,
    });
    expect(getTaskSidebarMeta({ ...job, enabled: false })).toMatchObject({
      canRun: false,
      canResume: true,
    });
    expect(getTaskSidebarMeta(taskJob())).toMatchObject({
      canEdit: false,
      canDelete: false,
    });
  });
  it("allows stopping and executing active scheduled tasks without destructive actions", () => {
    const meta = getTaskSidebarMeta(taskJob());

    expect(meta.state).toBe("active");
    expect(meta.canPause).toBe(true);
    expect(meta.canRun).toBe(true);
    expect(meta.canResume).toBe(false);
    expect(meta.canDelete).toBe(false);
    expect(meta.canEdit).toBe(false);
  });

  it("allows editing and deleting only when the scheduled task is disabled", () => {
    const meta = getTaskSidebarMeta(taskJob({ enabled: false }));

    expect(meta.canEdit).toBe(true);
    expect(meta.canDelete).toBe(true);
  });

  it("allows resuming and deleting paused scheduled tasks", () => {
    const meta = getTaskSidebarMeta(
      taskJob({
        enabled: false,
        task: {
          visible_in_my_tasks: true,
          has_scheduled_result: false,
          latest_scheduled_preview: "",
          unread_execution_count: 0,
          is_running: false,
          is_paused: true,
          pause_reason: "manual",
        },
      }),
    );

    expect(meta.state).toBe("manual-paused");
    expect(meta.canPause).toBe(false);
    expect(meta.canRun).toBe(false);
    expect(meta.canResume).toBe(true);
    expect(meta.canDelete).toBe(true);
    expect(meta.canEdit).toBe(true);
  });

  it("hides mutation actions while a task is running", () => {
    const meta = getTaskSidebarMeta(
      taskJob({
        task: {
          visible_in_my_tasks: true,
          has_scheduled_result: false,
          latest_scheduled_preview: "",
          unread_execution_count: 0,
          is_running: true,
          is_paused: false,
          pause_reason: null,
        },
      }),
    );

    expect(meta.state).toBe("running");
    expect(meta.canPause).toBe(false);
    expect(meta.canRun).toBe(false);
    expect(meta.canResume).toBe(false);
    expect(meta.canDelete).toBe(false);
    expect(meta.canEdit).toBe(false);
  });
});

describe("partitionTasksByPauseState", () => {
  it("keeps runnable and paused task order stable", () => {
    const active = taskJob({ id: "active" });
    const manualPaused = taskJob({
      id: "manual-paused",
      task: {
        visible_in_my_tasks: true,
        has_scheduled_result: false,
        latest_scheduled_preview: "",
        unread_execution_count: 0,
        is_running: false,
        is_paused: true,
        pause_reason: "manual",
      },
    });
    const running = taskJob({
      id: "running",
      task: {
        visible_in_my_tasks: true,
        has_scheduled_result: false,
        latest_scheduled_preview: "",
        unread_execution_count: 0,
        is_running: true,
        is_paused: false,
        pause_reason: null,
      },
    });
    const autoPaused = taskJob({
      id: "auto-paused",
      task: {
        visible_in_my_tasks: true,
        has_scheduled_result: false,
        latest_scheduled_preview: "",
        unread_execution_count: 3,
        is_running: false,
        is_paused: true,
        pause_reason: "auto_unread_threshold",
      },
    });

    const groups = partitionTasksByPauseState([
      active,
      manualPaused,
      running,
      autoPaused,
    ]);

    expect(groups.runnableTasks.map((task) => task.id)).toEqual([
      "active",
      "running",
    ]);
    expect(groups.pausedTasks.map((task) => task.id)).toEqual([
      "manual-paused",
      "auto-paused",
    ]);
  });

  it("returns empty groups for an empty collection", () => {
    expect(partitionTasksByPauseState([])).toEqual({
      runnableTasks: [],
      pausedTasks: [],
    });
  });
});

describe("getTaskPauseStatusText", () => {
  it("shows cleaned text when unread auto-pause count is cleared", () => {
    const meta = getTaskSidebarMeta(
      taskJob({
        enabled: false,
        task: {
          visible_in_my_tasks: true,
          has_scheduled_result: false,
          latest_scheduled_preview: "",
          unread_execution_count: 0,
          is_running: false,
          is_paused: true,
          pause_reason: "auto_unread_threshold",
        },
      }),
    );

    expect(getTaskPauseStatusText(meta)).toBe("已自动暂停 · 已清理");
  });

  it("shows unread count while unread auto-pause history remains", () => {
    const meta = getTaskSidebarMeta(
      taskJob({
        enabled: false,
        task: {
          visible_in_my_tasks: true,
          has_scheduled_result: false,
          latest_scheduled_preview: "",
          unread_execution_count: 3,
          is_running: false,
          is_paused: true,
          pause_reason: "auto_unread_threshold",
        },
      }),
    );

    expect(getTaskPauseStatusText(meta)).toBe("已自动暂停 · 连续 3 次未读");
  });
});

describe("getTaskNextRunTooltipText", () => {
  it("shows the next three run times from state", () => {
    const job = taskJob({
      state: {
        next_run_at: "2026-06-04T01:00:00Z",
        next_run_times: [
          "2026-06-04T01:00:00Z",
          "2026-06-05T01:00:00Z",
          "2026-06-06T01:00:00Z",
          "2026-06-07T01:00:00Z",
        ],
      },
    });
    const tooltip = getTaskNextRunTooltipText(job);

    expect(tooltip?.split("\n")).toHaveLength(4);
    expect(tooltip).toContain("之后三次运行时间");
    expect(tooltip).not.toContain("06-07");
    expect(getTaskNextRunTooltipTimes(job)).toHaveLength(3);
  });

  it("falls back to next_run_at when next_run_times is absent", () => {
    const tooltip = getTaskNextRunTooltipText(
      taskJob({
        state: {
          next_run_at: "2026-06-04T01:00:00Z",
        },
      }),
    );

    expect(tooltip).toContain("之后三次运行时间");
    expect(tooltip).toContain("06-04");
  });
});

describe("getTaskOpenTarget", () => {
  it("prefers task chat_id over compatibility session ids", () => {
    const target = getTaskOpenTarget(
      taskJob({
        task: {
          visible_in_my_tasks: true,
          has_scheduled_result: true,
          latest_scheduled_preview: "done",
          unread_execution_count: 1,
          is_running: false,
          is_paused: false,
          pause_reason: null,
          chat_id: "chat-outside-page",
          session_id: "task-session-id",
        },
        request: {
          input: [{ role: "user", content: "ping" }],
          session_id: "request-session-id",
        },
        dispatch: {
          type: "channel",
          channel: "console",
          target: {
            user_id: "user-1",
            session_id: "dispatch-session-id",
          },
        },
      }),
    );

    expect(target).toBe("chat-outside-page");
  });
});
