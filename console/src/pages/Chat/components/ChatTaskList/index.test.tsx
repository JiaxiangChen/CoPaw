import React from "react";
import { tagTask, taskIdentityKey } from "@/api/modules/cronTaskSources";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CronJobSpecOutput } from "@/api/types";
import ChatTaskList from ".";
import { formatListTime } from "../../listTimeFormat";

it("新版手动历史展示最近运行时间和未读次数，不依赖自动结果标志", () => {
  const task = tagTask(
    taskJob({
      state: { last_run_at: "2026-10-19 05:18:00", last_status: "success" },
      task: {
        visible_in_my_tasks: true,
        has_scheduled_result: false,
        latest_scheduled_preview: "",
        unread_execution_count: 2,
        is_running: false,
        is_paused: false,
      },
    }),
    "scheduler",
    { actor_id: "u", tenant_id: "u", source_id: "s", agent_id: "a" },
  );
  const { container } = render(<ChatTaskList tasks={[task]} />);
  expect(
    screen.getByText(formatListTime("2026-10-19T05:18:00Z")),
  ).toBeVisible();
  expect(
    container.querySelector(".chat-task-list-item-badge"),
  ).toHaveTextContent("2");
  expect(screen.getByText("已完成")).toBeVisible();
});

function taskJob(
  overrides: Partial<CronJobSpecOutput> = {},
): CronJobSpecOutput {
  return {
    id: "job-1",
    name: "每日巡检",
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

function pausedTask(
  id: string,
  unreadCount = 0,
  pauseReason: "manual" | "auto_unread_threshold" = "manual",
): CronJobSpecOutput {
  return taskJob({
    id,
    name: `暂停任务 ${id}`,
    enabled: false,
    task: {
      visible_in_my_tasks: true,
      has_scheduled_result: false,
      latest_scheduled_preview: "",
      unread_execution_count: unreadCount,
      is_running: false,
      is_paused: true,
      pause_reason: pauseReason,
    },
  });
}

describe("ChatTaskList actions", () => {
  it("新旧同 ID 任务不共享选中状态或 React key，动作保留真实来源", () => {
    const context = {
      actor_id: "u",
      tenant_id: "u",
      source_id: "s",
      agent_id: "a",
    };
    const oldTask = tagTask(taskJob({ name: "旧任务" }), "legacy", context);
    const newTask = tagTask(taskJob({ name: "新任务" }), "scheduler", context);
    const clicked = vi.fn();
    render(
      <ChatTaskList
        tasks={[oldTask, newTask]}
        selectedTaskId={taskIdentityKey(newTask)}
        onTaskClick={clicked}
      />,
    );
    const oldRow = screen.getByText("旧任务").closest(".chat-task-list-item");
    const newRow = screen.getByText("新任务").closest(".chat-task-list-item");
    expect(oldRow).not.toHaveClass("chat-task-list-item--selected");
    expect(newRow).toHaveClass("chat-task-list-item--selected");
    fireEvent.click(newRow!);
    expect(clicked).toHaveBeenCalledWith(newTask);
  });
  afterEach(() => {
    cleanup();
  });

  it("shows a non-interactive fallback when there are no tasks", () => {
    render(<ChatTaskList tasks={[]} />);

    expect(screen.getByText("暂无任务")).toBeInTheDocument();
    expect(
      screen.getByText("创建任务，让 AI 帮你自动推进"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "去创建" })).toBeNull();
    expect(screen.queryByRole("link", { name: "去创建" })).toBeNull();
  });

  it("opens stop and execute actions from the enabled task overflow menu", async () => {
    const onTaskClick = vi.fn();
    const onTaskPause = vi.fn();
    const onTaskRun = vi.fn();
    const onTaskDelete = vi.fn();
    const task = taskJob();

    render(
      <ChatTaskList
        tasks={[task]}
        onTaskClick={onTaskClick}
        onTaskPause={onTaskPause}
        onTaskRun={onTaskRun}
        onTaskDelete={onTaskDelete}
      />,
    );

    expect(screen.queryByText("停止")).toBeNull();
    expect(screen.queryByText("执行")).toBeNull();
    expect(screen.queryByText("删除")).toBeNull();

    fireEvent.click(
      screen.getByRole("button", { name: "更多任务操作：每日巡检" }),
    );
    fireEvent.click(await screen.findByText("停止"));

    fireEvent.click(
      screen.getByRole("button", { name: "更多任务操作：每日巡检" }),
    );
    fireEvent.click(await screen.findByText("执行"));

    fireEvent.click(
      screen.getByRole("button", { name: "更多任务操作：每日巡检" }),
    );
    await screen.findByText("停止");
    expect(screen.queryByText("删除")).toBeNull();

    expect(onTaskPause).toHaveBeenCalledWith(task);
    expect(onTaskRun).toHaveBeenCalledWith(task);
    expect(onTaskDelete).not.toHaveBeenCalled();
    expect(onTaskClick).not.toHaveBeenCalled();
  });

  it("hides edit for enabled scheduled tasks", async () => {
    const onTaskClick = vi.fn();
    const onTaskEdit = vi.fn();
    const task = taskJob();

    render(
      <ChatTaskList
        tasks={[task]}
        onTaskClick={onTaskClick}
        onTaskEdit={onTaskEdit}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "更多任务操作：每日巡检" }),
    );

    await screen.findByText("停止");
    expect(screen.queryByText("编辑")).toBeNull();

    expect(onTaskEdit).not.toHaveBeenCalled();
    expect(onTaskClick).not.toHaveBeenCalled();
  });

  it("opens edit and delete for a stopped task without selecting the task", async () => {
    const onTaskClick = vi.fn();
    const onTaskEdit = vi.fn();
    const onTaskDelete = vi.fn();
    const task = taskJob({ enabled: false });

    render(
      <ChatTaskList
        tasks={[task]}
        onTaskClick={onTaskClick}
        onTaskEdit={onTaskEdit}
        onTaskDelete={onTaskDelete}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "更多任务操作：每日巡检" }),
    );
    fireEvent.click(await screen.findByText("编辑"));

    fireEvent.click(
      screen.getByRole("button", { name: "更多任务操作：每日巡检" }),
    );
    fireEvent.click(await screen.findByText("删除"));

    expect(onTaskEdit).toHaveBeenCalledWith(task);
    expect(onTaskDelete).toHaveBeenCalledWith(task);
    expect(onTaskClick).not.toHaveBeenCalled();
  });

  it("opens resume and delete actions for paused scheduled tasks", async () => {
    const onTaskResume = vi.fn();
    const task = taskJob({
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
    });

    render(
      <ChatTaskList
        tasks={[task]}
        selectedTaskId={task.id}
        onTaskResume={onTaskResume}
        onTaskDelete={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "停止" })).toBeNull();
    expect(screen.queryByRole("button", { name: "执行" })).toBeNull();

    fireEvent.click(
      screen.getByRole("button", { name: "更多任务操作：每日巡检" }),
    );
    fireEvent.click(await screen.findByText("恢复"));

    await waitFor(() => {
      expect(screen.queryByText("停止")).toBeNull();
      expect(screen.queryByText("执行")).toBeNull();
    });

    expect(onTaskResume).toHaveBeenCalledWith(task);
  });

  it("collapses paused tasks by default without unread aggregation", () => {
    render(
      <ChatTaskList
        tasks={[
          taskJob({ id: "active", name: "正常任务" }),
          pausedTask("p1", 3),
        ]}
      />,
    );

    const toggle = screen.getByRole("button", { name: "已暂停任务 1" });
    const activeTask = screen.getByText("正常任务");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(activeTask).toBeVisible();
    expect(
      activeTask.compareDocumentPosition(toggle) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
    expect(screen.queryByText("暂停任务 p1")).not.toBeVisible();
    expect(toggle).not.toHaveTextContent("3");
  });

  it("expands and collapses paused tasks from the disclosure", () => {
    render(<ChatTaskList tasks={[pausedTask("p1")]} />);

    const toggle = screen.getByRole("button", { name: "已暂停任务 1" });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("暂停任务 p1")).toBeVisible();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("暂停任务 p1")).not.toBeVisible();
  });

  it("automatically expands when the selected task is paused", async () => {
    const task = pausedTask("selected");
    render(<ChatTaskList tasks={[task]} selectedTaskId={task.id} />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "已暂停任务 1" }),
      ).toHaveAttribute("aria-expanded", "true");
    });
    expect(screen.getByText("暂停任务 selected")).toBeVisible();
  });

  it("shows cleaned text for auto-paused tasks with cleared unread count", async () => {
    const task = pausedTask("cleaned", 0, "auto_unread_threshold");
    render(<ChatTaskList tasks={[task]} selectedTaskId={task.id} />);

    await waitFor(() => {
      expect(screen.getByText("已自动暂停 · 已清理")).toBeVisible();
    });
    expect(screen.queryByText("已自动暂停 · 连续 0 次未读")).toBeNull();
  });

  it("keeps the group collapsed when an unselected task becomes paused", () => {
    const active = taskJob({ id: "changing", name: "状态变化任务" });
    const { rerender } = render(<ChatTaskList tasks={[active]} />);

    rerender(<ChatTaskList tasks={[pausedTask("changing")]} />);

    expect(
      screen.getByRole("button", { name: "已暂停任务 1" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("暂停任务 changing")).not.toBeVisible();
  });

  it("does not render a paused disclosure without paused tasks", () => {
    render(<ChatTaskList tasks={[taskJob()]} />);
    expect(screen.queryByText(/已暂停任务/)).toBeNull();
  });

  it("shows completed status instead of scheduled result preview", () => {
    const task = taskJob({
      task: {
        visible_in_my_tasks: true,
        has_scheduled_result: true,
        latest_scheduled_preview: "这里是返回内容截取",
        unread_execution_count: 0,
        is_running: false,
        is_paused: false,
        pause_reason: null,
        last_scheduled_run_at: "2026-05-21T08:00:00Z",
      },
    });

    render(<ChatTaskList tasks={[task]} />);

    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.queryByText("这里是返回内容截取")).toBeNull();
  });

  it("shows unread badge without hiding task actions", () => {
    const task = taskJob({
      task: {
        visible_in_my_tasks: true,
        has_scheduled_result: true,
        latest_scheduled_preview: "",
        unread_execution_count: 3,
        is_running: false,
        is_paused: false,
        pause_reason: null,
      },
    });

    const { container } = render(<ChatTaskList tasks={[task]} />);

    expect(screen.getByText("3")).toBeInTheDocument();
    expect(
      container.querySelector(".chat-task-list-item-action-trigger"),
    ).toBeInTheDocument();
  });

  it("shows upcoming run times in a styled hover tooltip", async () => {
    const task = taskJob({
      state: {
        next_run_at: "2026-06-04T01:00:00Z",
        next_run_times: [
          "2026-06-04T01:00:00Z",
          "2026-06-05T01:00:00Z",
          "2026-06-06T01:00:00Z",
        ],
      },
    });
    const { container } = render(<ChatTaskList tasks={[task]} />);

    const nextRun = container.querySelector(".chat-task-list-item-next-run");

    expect(nextRun).not.toHaveAttribute("title");
    fireEvent.mouseEnter(nextRun as Element);

    await waitFor(() => {
      expect(screen.getByText("之后三次运行时间")).toBeInTheDocument();
    });
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent("1");
    expect(tooltip).toHaveTextContent("2");
    expect(tooltip).toHaveTextContent("3");
    expect(tooltip).toHaveTextContent("06-04");
    expect(tooltip).toHaveTextContent("06-05");
    expect(tooltip).toHaveTextContent("06-06");
  });
});
