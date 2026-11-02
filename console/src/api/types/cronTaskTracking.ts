export interface TaskIdentity {
  tenant_id: string;
  source_id: string;
  agent_id: string;
  job_id: string;
}
export interface TrackingMetadata {
  coverage: Record<string, "available" | "empty" | "unavailable">;
  warnings: string[];
  query_id: string;
  queried_at: string;
}
export interface TrackingCandidate extends TaskIdentity {
  name?: string | null;
  tenant_name?: string | null;
  deleted?: boolean;
  history_only?: boolean;
}
export interface TrackingLookup extends TrackingMetadata {
  candidates: TrackingCandidate[];
  requires_selection: boolean;
  has_more: boolean;
}
export interface TrackingOverview extends TrackingMetadata {
  identity: TaskIdentity;
  history_only: boolean;
  relation: "parent" | "child" | "ordinary" | "unknown";
  definition: Record<string, unknown> | null;
  state: Record<string, unknown> | null;
}
export type TrackingCategory =
  | "all"
  | "management"
  | "scheduling"
  | "execution"
  | "notification";
export type TrackingEntry =
  | "all"
  | "ordinary"
  | "batch"
  | "manual"
  | "manual_retry"
  | "daily_failure_retry"
  | "unknown"
  | "management";
export interface TrackingFilters {
  start_time?: string;
  end_time?: string;
  category?: TrackingCategory;
  entry?: TrackingEntry;
  order?: "asc" | "desc";
  limit?: number;
  cursor?: string;
}
export interface TrackingNode {
  event_id: string;
  node_type: string;
  category: Exclude<TrackingCategory, "all">;
  occurred_at: string;
  time_basis: string;
  title: string;
  recorded_outcome?: string | null;
  current_status?: string | null;
  entry: TrackingEntry;
  original_entry?: TrackingEntry | null;
  reason?: string | null;
  reason_basis?: string;
  execution_id?: string | null;
  operation_id?: string | null;
  history_id?: number | null;
  receipt_id?: string | null;
  actor_id?: string | null;
  actor_kind?: string;
  evidence?: string;
  source_refs: { table: string; key: string }[];
  details: Record<string, unknown>;
  gaps: string[];
}
export interface TrackingTimeline extends TrackingMetadata {
  identity: TaskIdentity;
  items: TrackingNode[];
  has_more: boolean;
  next_cursor: string | null;
  loaded_count: number;
  query_upper: string;
  consistency: string;
}
