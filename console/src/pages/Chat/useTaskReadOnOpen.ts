import { useEffect, useRef } from "react";
import type { CronJobSpecOutput } from "@/api/types";
import { cronAccessKey } from "@/api/cronTaskContext";
import { markSourcedTaskRead } from "@/api/modules/cronTaskActions";
import { taskIdentityKey, taskOrigin } from "@/api/modules/cronTaskSources";
import { shouldMarkTaskReadOnOpen } from "./taskJobs";
import { RequestIdentity } from "@/api/requestIdentity";

/** 只有已读接口成功才刷新水位；同一快照失败不反复写，也不清掉真实未读。 */
export function useTaskReadOnOpen(
  task: CronJobSpecOutput | null,
  onChanged: () => Promise<void> | void,
  onFailure: (message: string) => void,
) {
  const current = useRef(task);
  current.current = task;
  const pending = useRef(new Set<string>());
  const requests = useRef(new Map<string, RequestIdentity>());
  const stamp =
    task &&
    shouldMarkTaskReadOnOpen(task) &&
    (task.task?.unread_execution_count ?? 0) > 0
      ? JSON.stringify([
          taskIdentityKey(task),
          task.task?.unread_execution_count,
          task.task?.last_scheduled_run_at,
          taskOrigin(task)?.accessKey,
        ])
      : "";
  useEffect(() => {
    const job = current.current;
    if (!stamp || !job || pending.current.has(stamp)) return;
    const request = requests.current.get(stamp) ?? new RequestIdentity();
    requests.current.set(stamp, request);
    pending.current.add(stamp);
    let active = true;
    void markSourcedTaskRead(job, false, request.for(stamp))
      .then(async () => {
        request.reset();
        if (active && taskOrigin(job)?.accessKey === cronAccessKey())
          await onChanged();
      })
      .catch(() => {
        if (active && taskOrigin(job)?.accessKey === cronAccessKey())
          onFailure("标记已读失败，未读状态已保留，可重新打开后重试");
      })
      .finally(() => {
        pending.current.delete(stamp);
      });
    return () => {
      active = false;
    };
  }, [stamp, onChanged, onFailure]);
}
