import type {
  CronJobSpecInput,
  CronJobSpecOutput,
  CronJobState,
  CronTaskView,
} from "./cronjob";

/** 请求上下文固定真实操作者，目标工作区字段不代表授权。 */
export interface ManagedCronContext {
  actor_id: string;
  tenant_id: string;
  source_id: string;
  agent_id: string;
}

export type ManagedDispatchMode = "ordinary" | "batch_enabled" | "batch_paused";
export type ManagedCronSpec = Omit<CronJobSpecInput, "id"> & {
  id?: string;
  tenant_id?: string;
  source_id?: string;
  scope_id?: string;
  tenant_name?: string;
  bbk_id?: string;
  plan_id?: string;
};

export interface ManagedJob extends CronJobSpecOutput {
  tenant_id: string;
  source_id: string;
  agent_id: string;
  management_owner: "scheduler";
  definition_version: number;
  management_version: number | null;
  state_version: number | null;
  management_status: string;
  dispatch_state: ManagedDispatchMode;
  deleted: boolean;
  offset_minutes?: number;
  offset_window_hours?: number;
  enable_offset?: boolean;
}

export interface ManagedJobDetail
  extends Omit<
    ManagedJob,
    keyof CronJobSpecOutput | "tenant_id" | "source_id"
  > {
  spec: ManagedCronSpec & { id: string; tenant_id: string; source_id: string };
  state?: CronJobState;
  task?: CronTaskView | null;
}

export interface ManagedOperation {
  operation_id: string;
  job_id: string;
  action: string;
  status: "pending" | "running" | "completed" | "failed" | "superseded";
  stage: string;
  error_code?: string | null;
  version: number;
  expected_version: number;
  logical_delete_applied: boolean;
}

export interface ManagedExecution {
  execution_id: string;
  job_id: string;
  status: string;
  error_code?: string | null;
  output_preview?: string;
  /** Scheduler 观察主执行及异步结果后的整轮事实，不覆盖 SWE status。 */
  settled_status?: string | null;
  settled_at?: string | null;
  settled_error_code?: string | null;
}

export interface ManagedPage<T> {
  items: T[];
  next_cursor?: string | null;
}
export interface ManagedChild
  extends Omit<
    ManagedJob,
    "dispatch" | "runtime" | "request" | "text" | "meta"
  > {
  job_id: string;
  tenant_name?: string | null;
  bbk_id?: string | null;
}
export interface ManagedSelectedChild {
  job_id: string;
  tenant_id: string;
  agent_id: string;
  expected_definition_version: number;
}
export interface ManagedBroadcastRequest {
  request_id: string;
  expected_definition_version: number;
  targets: {
    tenant_id: string;
    tenant_name?: string | null;
    bbk_id?: string | null;
    agent_id?: string;
  }[];
  enable_offset: boolean;
  offset_window_hours: number;
}
export interface ManagedBroadcastItem {
  tenant_id: string;
  source_id: string;
  agent_id: string;
  job_id: string;
  status: string;
  operation: string;
  offset_minutes: number;
  cron: string;
  timezone: string;
  warning?: string | null;
  error_code?: string | null;
}
export interface ManagedBroadcastTask {
  task_id: string;
  source_job_id: string;
  status: string;
  tenant_count: number;
  success_count: number;
  failed_count: number;
  pending_count: number;
  sync_status: string;
  error_code?: string | null;
  reused?: boolean;
  results?: ManagedBroadcastItem[];
  next_cursor?: string | null;
}
export interface ManagedDispatchState {
  job_id: string;
  state: ManagedDispatchMode;
  version: number;
  effective_state: ManagedDispatchMode | "mixed";
  operation_id?: string | null;
  sync_status: string;
  error_code?: string | null;
  pending_count: number;
  completed_count: number;
  failed_count: number;
}
export interface ManagedBatchDeleteItem
  extends Omit<ManagedSelectedChild, "expected_definition_version"> {
  source_id: string;
  status: "completed" | "pending" | "failed";
  http_status: number;
  logical_delete_applied: boolean;
  operation_id?: string | null;
  stage?: string | null;
  error_code?: string | null;
}
export interface ManagedBatchDeleteResult {
  operation_id: string;
  operation_status: "pending" | "running" | "completed" | "failed";
  error_code?: string | null;
  all_children?: boolean;
  request_id: string;
  source_job_id: string;
  status: string;
  total_count: number;
  deleted_count: number;
  completed_count: number;
  pending_count: number;
  failed_count: number;
  results: ManagedBatchDeleteItem[];
}
