import { useCallback, useEffect, useRef, useState } from "react";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import {
  applyManagedTaskAction,
  assertTaskWorkspace,
  operationFromFailure,
  type ManagedTaskAction,
  type ManagedTaskActionResult,
} from "@/api/modules/cronTaskActions";
import { taskIdentityKey, taskOrigin } from "@/api/modules/cronTaskSources";
import type { SourcedManagedTask } from "@/api/types/cronTaskSource";
import type {
  ManagedCronSpec,
  ManagedJob,
  ManagedOperation,
} from "@/api/types/schedulerCron";
import { failureMessage } from "../Control/SchedulerCronJobs/helpers";
import { RequestIdentity } from "@/api/requestIdentity";

export interface ManagedTaskProgress extends ManagedTaskActionResult {
  job: SourcedManagedTask;
  scopeKey: string;
}

/** Chat 新任务操作不乐观伪造运行状态；未知结果复用原请求，失败保留操作 ID。 */
export function useManagedTaskActions(
  scopeKey: string,
  onChanged: () => Promise<void>,
  notify: (message: string, failed: boolean) => void,
) {
  const [progress, setProgress] = useState<ManagedTaskProgress | null>(null);
  const currentKey = useRef(scopeKey);
  currentKey.current = scopeKey;
  const alive = useRef(false);
  const generation = useRef(0);
  const requests = useRef(new Map<string, RequestIdentity>());
  const locks = useRef(new Set<string>());
  useEffect(() => {
    alive.current = true;
    generation.current += 1;
    requests.current.clear();
    locks.current.clear();
    return () => {
      alive.current = false;
    };
  }, [scopeKey]);
  const execute = useCallback(
    async (
      task: SourcedManagedTask,
      action: string,
      body: unknown,
      send: (requestId: string) => Promise<ManagedTaskActionResult>,
    ) => {
      const key = `${taskIdentityKey(task)}:${action}`;
      const requestGeneration = generation.current;
      const isCurrent = () =>
        alive.current &&
        currentKey.current === scopeKey &&
        generation.current === requestGeneration;
      if (
        !alive.current ||
        currentKey.current !== scopeKey ||
        locks.current.has(key)
      )
        return null;
      const request = requests.current.get(key) ?? new RequestIdentity();
      requests.current.set(key, request);
      locks.current.add(key);
      try {
        assertTaskWorkspace(task);
        const result = await send(request.for(body));
        request.reset();
        if (isCurrent()) {
          setProgress({ ...result, job: task, scopeKey });
          notify(
            result.execution
              ? "执行请求已受理，请查看实际执行进度"
              : "操作已受理，请查看同步进度",
            false,
          );
          await onChanged();
        }
        return isCurrent() ? result : null;
      } catch (error) {
        const operation = operationFromFailure(error);
        if (isCurrent()) {
          if (operation) setProgress({ job: task, operation, scopeKey });
          notify(failureMessage(error), true);
          await onChanged();
        }
        return isCurrent() && operation ? { operation } : null;
      } finally {
        if (isCurrent()) locks.current.delete(key);
      }
    },
    [scopeKey, notify, onChanged],
  );
  const perform = useCallback(
    (task: SourcedManagedTask, action: ManagedTaskAction) =>
      execute(
        task,
        action,
        {
          definition: task.definition_version,
          management: task.management_version,
        },
        (id) => applyManagedTaskAction(task, action, id),
      ),
    [execute],
  );
  const update = useCallback(
    async (
      original: SourcedManagedTask,
      current: ManagedJob,
      spec: ManagedCronSpec,
    ): Promise<ManagedOperation | null> => {
      const result = await execute(
        original,
        "update",
        { spec, version: current.definition_version },
        (id) =>
          schedulerCronApi.update(
            taskOrigin(original)!.context,
            current.id,
            spec,
            current.definition_version,
            id,
          ),
      );
      return result?.operation ?? null;
    },
    [execute],
  );
  const closeProgress = useCallback(() => setProgress(null), []);
  return {
    progress: progress?.scopeKey === scopeKey ? progress : null,
    closeProgress,
    perform,
    update,
  };
}
