import type { CronJobSpecOutput } from "../../api/types";
import { formatListTime } from "./listTimeFormat.ts";
import { isSchedulerTask } from "@/api/modules/cronTaskSources";

export interface TaskSidebarMeta {
  state: "active" | "running" | "auto-paused" | "manual-paused";
  unreadCount: number;
  canPause: boolean;
  canRun: boolean;
  canResume: boolean;
  canDelete: boolean;
  canEdit: boolean;
}

export interface TaskGroups {
  runnableTasks: CronJobSpecOutput[];
  pausedTasks: CronJobSpecOutput[];
}

const AUTO_PAUSE_REASON = "auto_unread_threshold";
export const TASK_COMPLETED_STATUS_TEXT = "已完成";

/** 新版已有手动运行时间时，不能因没有自动结果而提示尚未启动。 */
export function hasTaskRunRecord(job: CronJobSpecOutput): boolean {
  return Boolean(
    job.task?.has_scheduled_result ||
      (isSchedulerTask(job) &&
        (job.state?.last_run_at || job.task?.last_scheduled_run_at)),
  );
}

export function getTaskLastRunDisplay(
  job: CronJobSpecOutput,
): { at?: string; statusText: string } | null {
  const managed = isSchedulerTask(job);
  const latest = managed ? job.state?.last_run_at : null;
  let at = latest || job.task?.last_scheduled_run_at || undefined;
  if (!at && !job.task?.latest_scheduled_preview) return null;
  // 新状态表按 UTC 保存 DATETIME，接口未带时区时不能按浏览器本地时间解析。
  if (managed && at && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(at)) {
    at = at.replace(" ", "T") + "Z";
  }
  const states: Record<string, string> = {
    success: TASK_COMPLETED_STATUS_TEXT,
    running: "运行中",
    error: "执行失败",
    skipped: "已跳过",
    cancelled: "已取消",
  };
  return {
    at,
    statusText: latest
      ? states[job.state?.last_status || ""] || "已运行"
      : TASK_COMPLETED_STATUS_TEXT,
  };
}

export function isVisibleTask(job: CronJobSpecOutput): boolean {
  return Boolean(job.task?.visible_in_my_tasks);
}

export function getTaskSidebarMeta(job: CronJobSpecOutput): TaskSidebarMeta {
  if (isSchedulerTask(job)) return managedSidebarMeta(job);
  const unreadCount = Math.max(
    0,
    Number(job.task?.unread_execution_count || 0),
  );
  const pauseReason = job.task?.pause_reason;
  const isPaused = Boolean(job.task?.is_paused || pauseReason);
  const isRunning = Boolean(job.task?.is_running);

  if (isRunning) {
    return {
      state: "running",
      unreadCount,
      canPause: false,
      canRun: false,
      canResume: false,
      canDelete: false,
      canEdit: !job.enabled,
    };
  }

  if (pauseReason === AUTO_PAUSE_REASON) {
    return {
      state: "auto-paused",
      unreadCount,
      canPause: false,
      canRun: false,
      canResume: true,
      canDelete: !job.enabled,
      canEdit: !job.enabled,
    };
  }

  if (isPaused) {
    return {
      state: "manual-paused",
      unreadCount,
      canPause: false,
      canRun: false,
      canResume: true,
      canDelete: !job.enabled,
      canEdit: !job.enabled,
    };
  }

  return {
    state: "active",
    unreadCount,
    canPause: true,
    canRun: true,
    canResume: false,
    canDelete: !job.enabled,
    canEdit: !job.enabled,
  };
}

/** 新控制协议将停用、在途执行和内容编辑分开，旧任务仍沿用原规则。 */
function managedSidebarMeta(
  job: import("@/api/types/cronTaskSource").SourcedManagedTask,
): TaskSidebarMeta {
  const unreadCount = Math.max(
    0,
    Number(job.task?.unread_execution_count || 0),
  );
  const paused =
    !job.enabled || Boolean(job.task?.is_paused || job.task?.pause_reason);
  const running = Boolean(job.task?.is_running);
  const state = running
    ? "running"
    : job.task?.pause_reason === AUTO_PAUSE_REASON
    ? "auto-paused"
    : paused
    ? "manual-paused"
    : "active";
  return {
    state,
    unreadCount,
    canPause: !job.deleted && !paused,
    canRun: !job.deleted && !paused && !running,
    canResume: !job.deleted && paused,
    canDelete: !job.deleted,
    canEdit: !job.deleted && job.management_status === "ready",
  };
}

export function partitionTasksByPauseState(
  jobs: CronJobSpecOutput[],
): TaskGroups {
  return jobs.reduce<TaskGroups>(
    (groups, job) => {
      const state = getTaskSidebarMeta(job).state;
      if (state === "auto-paused" || state === "manual-paused") {
        groups.pausedTasks.push(job);
      } else {
        groups.runnableTasks.push(job);
      }
      return groups;
    },
    { runnableTasks: [], pausedTasks: [] },
  );
}

export function getTaskPauseStatusText(
  sidebarMeta: TaskSidebarMeta,
): string | null {
  if (sidebarMeta.state === "auto-paused") {
    if (sidebarMeta.unreadCount <= 0) {
      return "已自动暂停 · 已清理";
    }
    return `已自动暂停 · 连续 ${sidebarMeta.unreadCount} 次未读`;
  }
  if (sidebarMeta.state === "manual-paused") {
    return "已手动暂停";
  }
  return null;
}

export function shouldMarkTaskReadOnOpen(job: CronJobSpecOutput): boolean {
  return !getTaskSidebarMeta(job).canResume;
}

export function getTaskNextRunText(job: CronJobSpecOutput): string | null {
  const sidebarMeta = getTaskSidebarMeta(job);
  if (sidebarMeta.state === "running") {
    return "运行中...";
  }

  if (sidebarMeta.canResume) {
    return null;
  }

  const formatted = formatListTime(job.state?.next_run_at);
  if (!formatted) {
    return null;
  }

  return `下次运行：${formatted}`;
}

export function getTaskNextRunTooltipText(
  job: CronJobSpecOutput,
): string | undefined {
  const runTimes = getTaskNextRunTooltipTimes(job);

  if (runTimes.length === 0) {
    return undefined;
  }

  return ["之后三次运行时间", ...runTimes].join("\n");
}

export function getTaskNextRunTooltipTimes(job: CronJobSpecOutput): string[] {
  const runTimes = (job.state?.next_run_times || [])
    .map(formatListTime)
    .filter(Boolean)
    .slice(0, 3);

  if (runTimes.length === 0) {
    const nextRun = formatListTime(job.state?.next_run_at);
    if (nextRun) {
      runTimes.push(nextRun);
    }
  }

  return runTimes;
}

export function getTaskOpenTarget(job: CronJobSpecOutput): string | null {
  const normalize = (value: string | null | undefined): string | null => {
    const text = String(value || "").trim();
    return text || null;
  };

  return (
    normalize(job.task?.chat_id) ||
    normalize(job.task?.session_id) ||
    normalize(job.request?.session_id as string | null | undefined) ||
    normalize(job.dispatch?.target?.session_id)
  );
}

export function deriveChatTaskState(
  jobs: CronJobSpecOutput[],
  chatId: string | undefined,
): {
  tasks: CronJobSpecOutput[];
  currentTask: CronJobSpecOutput | null;
} {
  return {
    tasks: jobs.filter(isVisibleTask),
    currentTask: currentTaskForChat(jobs, chatId),
  };
}

/** 裸 ID 不能在新旧来源撞号时静默选中其中一个。 */
export function uniqueTaskForId(
  jobs: CronJobSpecOutput[],
  id: string,
): CronJobSpecOutput | null {
  const matches = jobs.filter((job) => job.id === id);
  return matches.length === 1 ? matches[0] : null;
}

function currentTaskForChat(
  jobs: CronJobSpecOutput[],
  chatId: string | undefined,
): CronJobSpecOutput | null {
  if (!chatId) return null;
  const matches = jobs.filter((job) => job.task?.chat_id === chatId);
  return matches.length === 1 ? matches[0] : null;
}
