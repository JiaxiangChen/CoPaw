import { request } from "../request";
import type {
  ManagedBatchDeleteResult,
  ManagedBroadcastRequest,
  ManagedBroadcastTask,
  ManagedChild,
  ManagedCronContext,
  ManagedCronSpec,
  ManagedDispatchMode,
  ManagedDispatchState,
  ManagedExecution,
  ManagedJob,
  ManagedJobDetail,
  ManagedOperation,
  ManagedPage,
  ManagedSelectedChild,
} from "../types/schedulerCron";

const ROOT = "/scheduler/cron/jobs";
const jobPath = (id: string) => `${ROOT}/${encodeURIComponent(id)}`;
const SPEC_FIELDS = new Set([
  "name",
  "enabled",
  "schedule",
  "task_type",
  "text",
  "skill_ids",
  "workflow_binding_id",
  "model_slot",
  "request",
  "dispatch",
  "runtime",
  "meta",
  "tenant_id",
  "source_id",
  "scope_id",
  "tenant_name",
  "bbk_id",
  "plan_id",
]);

function definitionOnly(spec: ManagedCronSpec): ManagedCronSpec {
  return Object.fromEntries(
    Object.entries(spec).filter(([key]) => SPEC_FIELDS.has(key)),
  ) as ManagedCronSpec;
}

/** 只改变目标定位字段，绝不从任务记录推导操作者或 manager 角色。 */
export function targetManagedContext(
  context: ManagedCronContext,
  target: Pick<ManagedCronContext, "tenant_id" | "source_id" | "agent_id">,
): ManagedCronContext {
  return {
    ...context,
    tenant_id: target.tenant_id,
    source_id: target.source_id,
    agent_id: target.agent_id,
  };
}

function headers(context: ManagedCronContext): HeadersInit {
  return {
    "X-User-Id": context.actor_id,
    "X-Tenant-Id": context.tenant_id,
    "X-Source-Id": context.source_id,
    "X-Agent-Id": context.agent_id,
    "X-Scope-Id": `${context.tenant_id}-${context.source_id}`,
  };
}

function call<T>(
  context: ManagedCronContext,
  path: string,
  options: RequestInit = {},
): Promise<T> {
  // 认证凭据仍由统一请求层实时构建，不把旧 token 固定到上下文快照。
  return request<T>(path, { ...options, headers: headers(context) });
}

function write<T>(
  context: ManagedCronContext,
  path: string,
  method: string,
  body: unknown,
): Promise<T> {
  return call<T>(context, path, { method, body: JSON.stringify(body) });
}

async function allPages<T>(
  context: ManagedCronContext,
  path: string,
  signal?: AbortSignal,
): Promise<T[]> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null | undefined;
  do {
    const url = new URLSearchParams({ limit: "200" });
    if (cursor) url.set("cursor", cursor);
    const page = await call<ManagedPage<T>>(
      context,
      `${path}${path.includes("?") ? "&" : "?"}${url}`,
      { signal },
    );
    items.push(...page.items);
    cursor = page.next_cursor;
    if (cursor && seen.has(cursor))
      throw new Error("新版任务分页游标重复，请刷新后重试");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return items;
}

/** 新任务协议独立封装，失败只抛错，不调用 SWE 旧任务写接口。 */
export const schedulerCronApi = {
  listAll: (
    context: ManagedCronContext,
    options: { includeDeleted?: boolean; signal?: AbortSignal } = {},
  ) =>
    allPages<ManagedJob>(
      context,
      `${ROOT}?include_deleted=${Boolean(options.includeDeleted)}`,
      options.signal,
    ),
  detail: (context: ManagedCronContext, id: string, includeDeleted = false) =>
    call<ManagedJobDetail>(
      context,
      `${jobPath(id)}?include_deleted=${includeDeleted}`,
    ),
  create: (
    context: ManagedCronContext,
    spec: ManagedCronSpec,
    requestId: string,
    mode: ManagedDispatchMode = "ordinary",
  ) => {
    return write<{ operation: ManagedOperation }>(context, ROOT, "POST", {
      request_id: requestId,
      spec: definitionOnly(spec),
      dispatch_state: mode,
    });
  },
  update: (
    context: ManagedCronContext,
    id: string,
    spec: ManagedCronSpec,
    version: number,
    requestId: string,
  ) =>
    write<{ operation: ManagedOperation }>(context, jobPath(id), "PUT", {
      request_id: requestId,
      expected_definition_version: version,
      spec: definitionOnly(spec),
    }),
  remove: (
    context: ManagedCronContext,
    id: string,
    version: number,
    requestId: string,
  ) =>
    write<{ operation: ManagedOperation }>(context, jobPath(id), "DELETE", {
      request_id: requestId,
      expected_definition_version: version,
    }),
  setEnabled: (
    context: ManagedCronContext,
    id: string,
    enabled: boolean,
    version: number,
    requestId: string,
  ) =>
    write<{ operation: ManagedOperation }>(
      context,
      `${jobPath(id)}/enabled`,
      "PUT",
      { request_id: requestId, enabled, expected_management_version: version },
    ),
  markRead: (context: ManagedCronContext, id: string, requestId: string) =>
    write<{ operation: ManagedOperation }>(
      context,
      `${jobPath(id)}/read`,
      "POST",
      { request_id: requestId },
    ),
  run: (
    context: ManagedCronContext,
    id: string,
    version: number,
    requestId: string,
  ) =>
    write<{ execution: ManagedExecution; reused: boolean }>(
      context,
      `${jobPath(id)}/run`,
      "POST",
      { request_id: requestId, expected_definition_version: version },
    ),
  execution: (context: ManagedCronContext, id: string, executionId: string) =>
    call<ManagedExecution>(
      context,
      `${jobPath(id)}/executions/${encodeURIComponent(executionId)}`,
    ),
  operation: (context: ManagedCronContext, id: string, operationId: string) =>
    call<ManagedOperation>(
      context,
      `${jobPath(id)}/operations/${encodeURIComponent(operationId)}`,
    ),
  pending: (context: ManagedCronContext, id: string) =>
    call<{ operation: ManagedOperation | null }>(
      context,
      `${jobPath(id)}/operations/pending`,
    ),
  retry: (context: ManagedCronContext, id: string, operationId: string) =>
    call<{ operation: ManagedOperation }>(
      context,
      `${jobPath(id)}/operations/${encodeURIComponent(operationId)}/retry`,
      { method: "POST" },
    ),
  broadcast: (
    context: ManagedCronContext,
    id: string,
    body: ManagedBroadcastRequest,
  ) =>
    write<ManagedBroadcastTask>(
      context,
      `${jobPath(id)}/broadcast`,
      "POST",
      body,
    ),
  currentBroadcast: (context: ManagedCronContext, id: string) =>
    call<{ task: ManagedBroadcastTask | null }>(
      context,
      `${jobPath(id)}/broadcast/tasks/current?limit=200`,
    ),
  broadcastTask: (
    context: ManagedCronContext,
    id: string,
    taskId: string,
    cursor?: string,
  ) =>
    call<ManagedBroadcastTask>(
      context,
      `${jobPath(id)}/broadcast/tasks/${encodeURIComponent(taskId)}?limit=200${
        cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""
      }`,
    ),
  dispatchState: (context: ManagedCronContext, id: string) =>
    call<ManagedDispatchState>(context, `${jobPath(id)}/dispatch-state`),
  setDispatchState: (
    context: ManagedCronContext,
    id: string,
    mode: ManagedDispatchMode,
    version: number,
    requestId: string,
  ) =>
    write<ManagedDispatchState>(
      context,
      `${jobPath(id)}/dispatch-state`,
      "PUT",
      { request_id: requestId, state: mode, expected_version: version },
    ),
  priority: (context: ManagedCronContext, id: string) =>
    call<{
      priority: { user_ids: string[]; branch_ids: string[] };
      management_version: number;
    }>(context, `${jobPath(id)}/batch-dispatch/priority`),
  setPriority: (
    context: ManagedCronContext,
    id: string,
    users: string[],
    branches: string[],
    version: number,
    requestId: string,
  ) =>
    write<{ operation: ManagedOperation }>(
      context,
      `${jobPath(id)}/batch-dispatch/priority`,
      "PUT",
      {
        request_id: requestId,
        expected_management_version: version,
        user_ids: users,
        branch_ids: branches,
      },
    ),
  childrenAll: (
    context: ManagedCronContext,
    id: string,
    includeDeleted = false,
    signal?: AbortSignal,
  ) =>
    allPages<ManagedChild>(
      context,
      `${jobPath(id)}/broadcast/children?include_deleted=${includeDeleted}`,
      signal,
    ),
  childrenDelete: (
    context: ManagedCronContext,
    id: string,
    items: ManagedSelectedChild[],
    requestId: string,
  ) =>
    write<ManagedBatchDeleteResult>(
      context,
      `${jobPath(id)}/broadcast/children/batch-delete`,
      "POST",
      { request_id: requestId, items },
    ),
  childrenDeleteAll: (
    context: ManagedCronContext,
    id: string,
    items: ManagedSelectedChild[],
    requestId: string,
  ) =>
    write<ManagedBatchDeleteResult>(
      context,
      `${jobPath(id)}/broadcast/children/delete-all`,
      "POST",
      { request_id: requestId, items },
    ),
  currentChildrenDeletion: (context: ManagedCronContext, id: string) =>
    call<{ task: ManagedBatchDeleteResult | null }>(
      context,
      `${jobPath(id)}/broadcast/children/batch-delete/tasks/current`,
    ),
  childrenDeletion: (
    context: ManagedCronContext,
    id: string,
    operationId: string,
  ) =>
    call<ManagedBatchDeleteResult>(
      context,
      `${jobPath(
        id,
      )}/broadcast/children/batch-delete/tasks/${encodeURIComponent(
        operationId,
      )}`,
    ),
  retryChildrenDeletion: (
    context: ManagedCronContext,
    id: string,
    operationId: string,
  ) =>
    write<ManagedBatchDeleteResult>(
      context,
      `${jobPath(
        id,
      )}/broadcast/children/batch-delete/tasks/${encodeURIComponent(
        operationId,
      )}/retry`,
      "POST",
      {},
    ),
  childrenRun: (
    context: ManagedCronContext,
    id: string,
    items: ManagedSelectedChild[],
    requestId: string,
  ) =>
    write<{
      results: {
        status: string;
        error_code?: string;
        execution?: ManagedExecution;
      }[];
      failed_count: number;
    }>(context, `${jobPath(id)}/broadcast/children/actions`, "POST", {
      request_id: requestId,
      action: "run",
      items,
    }),
};
