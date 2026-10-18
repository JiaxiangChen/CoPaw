interface SessionLike {
  id?: string;
  meta?: Record<string, unknown> | null;
}

export function filterStaleTaskSessions<T extends SessionLike>(
  sessions: T[],
  activeTaskJobIds: ReadonlySet<string> | TaskPresence | null,
): T[] {
  if (activeTaskJobIds === null) {
    return [...sessions];
  }

  return sessions.filter((session) => {
    const meta = session.meta ?? {};
    if (meta.session_kind !== "task") {
      return true;
    }

    const taskJobId = String(meta.task_job_id ?? "").trim();
    if (!taskJobId) return false;
    if ("has" in activeTaskJobIds) return activeTaskJobIds.has(taskJobId);
    const owner = meta.management_owner;
    if (owner === "scheduler")
      return (
        activeTaskJobIds.scheduler === null ||
        activeTaskJobIds.scheduler.has(taskJobId)
      );
    if (owner === "legacy")
      return (
        activeTaskJobIds.legacy === null ||
        activeTaskJobIds.legacy.has(taskJobId)
      );
    // 老会话没有归属标记；任一来源未确认时保守保留，不能用失败清单删会话。
    return (
      activeTaskJobIds.legacy === null ||
      activeTaskJobIds.scheduler === null ||
      activeTaskJobIds.legacy.has(taskJobId) ||
      activeTaskJobIds.scheduler.has(taskJobId)
    );
  });
}
import type { TaskPresence } from "@/api/types/cronTaskSource";
