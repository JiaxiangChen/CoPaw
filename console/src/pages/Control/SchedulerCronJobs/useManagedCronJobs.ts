import { useCallback, useEffect, useRef, useState } from "react";
import {
  schedulerCronApi,
  targetManagedContext,
} from "@/api/modules/schedulerCron";
import type {
  ManagedCronContext,
  ManagedCronSpec,
  ManagedExecution,
  ManagedJob,
  ManagedOperation,
} from "@/api/types/schedulerCron";

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : "新版任务请求失败";
}

function operationFromError(error: unknown): ManagedOperation | null {
  if (!error || typeof error !== "object" || !("data" in error)) return null;
  const data = error.data;
  if (!data || typeof data !== "object" || !("operation" in data)) return null;
  const operation = data.operation;
  if (
    !operation ||
    typeof operation !== "object" ||
    !("operation_id" in operation) ||
    !("job_id" in operation) ||
    !("status" in operation)
  )
    return null;
  return typeof operation.operation_id === "string" &&
    typeof operation.job_id === "string" &&
    typeof operation.status === "string"
    ? (operation as ManagedOperation)
    : null;
}

/** 页面只缓存展示结果；身份变化清空旧视图，所有写入仍以服务器版本校验为准。 */
export function useManagedCronJobs(
  context: ManagedCronContext,
  includeDeleted = false,
  permissionRevision = "",
) {
  const scopeKey = JSON.stringify([
    context.actor_id,
    context.tenant_id,
    context.source_id,
    context.agent_id,
    permissionRevision,
    includeDeleted,
  ]);
  const contextRef = useRef(context);
  contextRef.current = context;
  const currentKey = useRef(scopeKey);
  currentKey.current = scopeKey;
  const mounted = useRef(false);
  const sequence = useRef(0);
  const writeLock = useRef<string | null>(null);
  const [jobs, setJobs] = useState<ManagedJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [operation, setOperation] = useState<ManagedOperation | null>(null);
  const [execution, setExecution] = useState<ManagedExecution | null>(null);

  const reload = useCallback(
    async (signal?: AbortSignal) => {
      const serial = ++sequence.current;
      const snapshot = { ...contextRef.current };
      setLoading(true);
      try {
        const data = await schedulerCronApi.listAll(snapshot, {
          includeDeleted,
          signal,
        });
        if (
          mounted.current &&
          currentKey.current === scopeKey &&
          serial === sequence.current
        ) {
          setJobs(data);
          setError(null);
        }
      } catch (failure) {
        if (
          !signal?.aborted &&
          mounted.current &&
          currentKey.current === scopeKey &&
          serial === sequence.current
        )
          setError(messageOf(failure));
      } finally {
        if (
          mounted.current &&
          currentKey.current === scopeKey &&
          serial === sequence.current
        )
          setLoading(false);
      }
    },
    [scopeKey, includeDeleted],
  );

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    setJobs([]);
    setOperation(null);
    setExecution(null);
    setError(null);
    setBusy(false);
    void reload(controller.signal);
    return () => {
      mounted.current = false;
      controller.abort();
    };
  }, [reload]);

  const perform = useCallback(
    async (
      send: (
        snapshot: ManagedCronContext,
      ) => Promise<{ operation: ManagedOperation }>,
    ) => {
      if (
        !mounted.current ||
        currentKey.current !== scopeKey ||
        writeLock.current === scopeKey
      )
        return null;
      writeLock.current = scopeKey;
      setBusy(true);
      setError(null);
      try {
        const result = await send({ ...contextRef.current });
        if (mounted.current && currentKey.current === scopeKey) {
          await reload();
          if (currentKey.current === scopeKey) setOperation(result.operation);
        }
        return result.operation;
      } catch (failure) {
        const progress = operationFromError(failure);
        if (mounted.current && currentKey.current === scopeKey) {
          await reload();
          if (currentKey.current === scopeKey) {
            setOperation(progress);
            setError(messageOf(failure));
          }
        }
        return progress;
      } finally {
        if (writeLock.current === scopeKey) writeLock.current = null;
        if (mounted.current && currentKey.current === scopeKey) setBusy(false);
      }
    },
    [reload, scopeKey],
  );

  const run = useCallback(
    async (job: ManagedJob, requestId: string) => {
      const key = scopeKey;
      if (
        !mounted.current ||
        currentKey.current !== key ||
        writeLock.current === key
      )
        return null;
      writeLock.current = key;
      setBusy(true);
      try {
        const result = await schedulerCronApi.run(
          targetManagedContext(contextRef.current, job),
          job.id,
          job.definition_version,
          requestId,
        );
        if (mounted.current && currentKey.current === key) {
          setExecution(result.execution);
          await reload();
        }
        return result.execution;
      } catch (failure) {
        if (mounted.current && currentKey.current === key)
          setError(messageOf(failure));
        return null;
      } finally {
        if (writeLock.current === key) writeLock.current = null;
        if (mounted.current && currentKey.current === key) setBusy(false);
      }
    },
    [scopeKey, reload],
  );

  /** 编辑读取最新详情后同步旧列表快照，不额外发一次可能滞后的列表请求。 */
  const reconcileJob = useCallback(
    (fresh: ManagedJob) => {
      if (!mounted.current || currentKey.current !== scopeKey) return;
      ++sequence.current;
      setLoading(false);
      setJobs((previous) =>
        previous.map((item) =>
          item.id === fresh.id &&
          item.tenant_id === fresh.tenant_id &&
          item.source_id === fresh.source_id &&
          item.agent_id === fresh.agent_id &&
          item.definition_version <= fresh.definition_version
            ? fresh
            : item,
        ),
      );
    },
    [scopeKey],
  );

  return {
    jobs,
    loading,
    busy,
    error,
    operation,
    execution,
    reload,
    reconcileJob,
    setOperation,
    setExecution,
    create: (spec: ManagedCronSpec, requestId: string) =>
      perform((ctx) => schedulerCronApi.create(ctx, spec, requestId)),
    update: (job: ManagedJob, spec: ManagedCronSpec, requestId: string) =>
      perform((ctx) =>
        schedulerCronApi.update(
          targetManagedContext(ctx, job),
          job.id,
          spec,
          job.definition_version,
          requestId,
        ),
      ),
    remove: (job: ManagedJob, requestId: string) =>
      perform((ctx) =>
        schedulerCronApi.remove(
          targetManagedContext(ctx, job),
          job.id,
          job.definition_version,
          requestId,
        ),
      ),
    toggle: (job: ManagedJob, requestId: string) =>
      perform((ctx) => {
        if (!job.management_version)
          throw new Error("任务状态缺失，请先刷新并核对数据库");
        return schedulerCronApi.setEnabled(
          targetManagedContext(ctx, job),
          job.id,
          Boolean(job.task?.is_paused) || !job.enabled,
          job.management_version,
          requestId,
        );
      }),
    retry: (job: ManagedJob, operationId: string) =>
      perform((ctx) =>
        schedulerCronApi.retry(
          targetManagedContext(ctx, job),
          job.id,
          operationId,
        ),
      ),
    run,
  };
}
