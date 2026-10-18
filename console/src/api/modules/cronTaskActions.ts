import type { CronJobSpecOutput } from "../types/cronjob";
import type { SourcedManagedTask } from "../types/cronTaskSource";
import type { CronTaskOrigin } from "../types/cronTaskSource";
import type {
  ManagedExecution,
  ManagedOperation,
} from "../types/schedulerCron";
import { cronAccessKey } from "../cronTaskContext";
import { taskOrigin } from "./cronTaskSources";
import { schedulerCronApi } from "./schedulerCron";
import { cronJobApi } from "./cronjob";

export type ManagedTaskAction = "pause" | "resume" | "run" | "delete";
export interface ManagedTaskActionResult {
  operation?: ManagedOperation;
  execution?: ManagedExecution;
}

export function assertTaskWorkspace(task: CronJobSpecOutput): void {
  const origin = taskOrigin(task);
  if (!origin || origin.accessKey !== cronAccessKey())
    throw new Error("任务工作区或权限已变化，请刷新后重试");
}

/** 版本与动作分别匹配；失败直接上抛，绝不尝试旧写接口。 */
export async function applyManagedTaskAction(
  task: SourcedManagedTask,
  action: ManagedTaskAction,
  requestId: string,
): Promise<ManagedTaskActionResult> {
  assertTaskWorkspace(task);
  const origin = taskOrigin(task)!;
  if (origin.owner !== "scheduler") throw new Error("任务不是新版管理任务");
  const context = origin.context;
  if (action === "run")
    return schedulerCronApi.run(
      context,
      task.id,
      task.definition_version,
      requestId,
    );
  if (action === "delete")
    return schedulerCronApi.remove(
      context,
      task.id,
      task.definition_version,
      requestId,
    );
  if (!task.management_version) throw new Error("任务管理版本缺失，请刷新核对");
  return schedulerCronApi.setEnabled(
    context,
    task.id,
    action === "resume",
    task.management_version,
    requestId,
  );
}

export async function markSourcedTaskRead(
  task: CronJobSpecOutput,
  markedRead: boolean,
  requestId: string,
): Promise<unknown> {
  assertTaskWorkspace(task);
  const origin = taskOrigin(task)!;
  return markTaskReferenceRead(task.id, origin, markedRead, requestId);
}

/** 报告使用已确认的来源引用，不能把裸 ID 默认当作旧任务。 */
export async function markTaskReferenceRead(
  id: string,
  origin: CronTaskOrigin,
  markedRead: boolean,
  requestId: string,
): Promise<unknown> {
  if (origin.accessKey !== cronAccessKey())
    throw new Error("报告工作区或权限已变化，未标记已读");
  if (origin.owner === "legacy") return cronJobApi.markTaskRead(id, markedRead);
  const result = await schedulerCronApi.markRead(origin.context, id, requestId);
  if (result.operation.status !== "completed")
    throw new Error(`已读处理尚未完成：${result.operation.operation_id}`);
  return result;
}

export function operationFromFailure(error: unknown): ManagedOperation | null {
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
