import { request } from "../request";
import type { CronDispatchWorkersResponse } from "./monitor";

export interface ManagedBatch {
  batch_id: string;
  tenant_id: string;
  source_id: string;
  agent_id: string;
  job_id: string;
  job_name: string | null;
  status: string;
  member_count: number;
  done_count: number;
  failed_count: number;
  success_count?: number;
  skipped_count?: number;
  provider_id?: string | null;
  model_id?: string | null;
  dispatch_paused?: boolean;
  lock_owner?: string | null;
  active_count: number;
  waiting_count: number;
  completion_rate: number | null;
  logical_fire_at: string;
  physical_fire_at: string;
  created_at: string;
}
export interface ManagedBatchMember {
  member_id: string;
  execution_id: string;
  tenant_id: string;
  agent_id: string;
  job_id: string;
  job_name: string | null;
  business_attempt: number;
  member_role: string;
  member_order: number;
  status: string;
  settled_status: string | null;
  error_code: string | null;
  settled_error_code: string | null;
  error_message?: string;
  provider_id: string;
  model_id: string;
  definition_version: number;
  current_definition_version: number;
  dispatch_attempt: number;
  actor_id: string;
  management_status: string;
  dispatch_state: string;
  bbk_id: string | null;
  not_before_at: string | null;
  accepted_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  settled_at: string | null;
  dispatch_priority: Record<string, unknown> | null;
  viewer_heat_score: number | null;
}
export interface ManagedBatchEvent {
  event_id: string;
  event_type: string;
  execution_id: string | null;
  member_id: string | null;
  actor_id: string;
  occurred_at: string;
  details: Record<string, unknown>;
}
export interface ManagedBatchList {
  items: ManagedBatch[];
  total: number;
  page: number;
  page_size: number;
  stats: {
    total_batches: number;
    total_members: number;
    done_members: number;
    failed_members: number;
    running_batches: number;
  };
}
export interface ManagedBatchDetail {
  batch: ManagedBatch;
  members: ManagedBatchMember[];
  member_total: number;
  member_page: number;
  member_page_size: number;
  events: ManagedBatchEvent[];
  event_total: number;
  event_page: number;
  event_page_size: number;
}
export interface RetryCandidate {
  member_id: string;
  execution_id: string;
  member_version: number;
  execution_version: number;
  definition_version: number;
  business_attempt: number;
}
export interface FailureMember extends RetryCandidate {
  error_message?: string;
  error_code?: string | null;
  settled_error_code?: string | null;
  tenant_id: string;
  job_id: string;
  failure_type: string;
  status: string;
  transport_only: boolean;
  execution_definition_version: number;
}
export interface ManagedFailurePreview {
  counts: { failure_type: string; label: string; count: number }[];
  items: FailureMember[];
  matched_count: number;
  has_more: boolean;
}
export interface ManagedRetryBody {
  request_id: string;
  candidates: RetryCandidate[];
  confirm_stopped: boolean;
}
export interface ManagedRetryResult {
  queued: number;
  skipped: number;
  reused: boolean;
  results: {
    member_id: string;
    execution_id?: string;
    status: string;
    reason?: string;
    transport_only?: boolean;
  }[];
}
type Filters = Record<string, string | number | undefined>;
const MONITOR = "/monitor/cron/managed-dispatch";
const SCHEDULER = "/scheduler/cron/managed-dispatch/batches";
const idPath = (id: string) => encodeURIComponent(id);
function call<T>(
  source: string,
  path: string,
  filters: Filters = {},
  signal?: AbortSignal,
) {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(filters))
    if (value !== undefined && value !== "") params.set(name, String(value));
  return request<T>(`${path}?${params}`, {
    headers: { "X-Source-Id": source },
    signal,
  });
}
/** 所有权限 Header 仍由统一认证层实时产生；不从批次记录推导角色。 */
export const managedBatchBoard = {
  batches: (source: string, filters: Filters, signal?: AbortSignal) =>
    call<ManagedBatchList>(source, `${MONITOR}/batches`, filters, signal),
  detail: (
    source: string,
    batch: string,
    filters: Filters,
    signal?: AbortSignal,
  ) =>
    call<ManagedBatchDetail>(
      source,
      `${MONITOR}/batches/${idPath(batch)}`,
      filters,
      signal,
    ),
  workers: (source: string, filters: Filters, signal?: AbortSignal) =>
    call<CronDispatchWorkersResponse>(
      source,
      `${MONITOR}/workers`,
      filters,
      signal,
    ),
  failures: (
    source: string,
    batch: string,
    types: string[],
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams();
    types.forEach((type) => params.append("types", type));
    return request<ManagedFailurePreview>(
      `${SCHEDULER}/${idPath(batch)}/failures?${params}`,
      { headers: { "X-Source-Id": source }, signal },
    );
  },
  retry: (source: string, batch: string, body: ManagedRetryBody) =>
    request<ManagedRetryResult>(`${SCHEDULER}/${idPath(batch)}/retry`, {
      method: "POST",
      headers: { "X-Source-Id": source },
      body: JSON.stringify(body),
    }),
};

export function retryCandidates(items: FailureMember[]): RetryCandidate[] {
  return items.map(
    ({
      member_id,
      execution_id,
      member_version,
      execution_version,
      definition_version,
      business_attempt,
    }) => ({
      member_id,
      execution_id,
      member_version,
      execution_version,
      definition_version,
      business_attempt,
    }),
  );
}
