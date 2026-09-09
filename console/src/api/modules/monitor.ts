import { request } from "../request";
import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";

// Types for Monitor Cron Overview

export interface FilterOption {
  value: string;
  label: string;
}

export interface FilterOptionsResponse {
  users: FilterOption[];
  bbk_ids: FilterOption[];
  channels: FilterOption[];
  source_ids: FilterOption[];
  job_names: FilterOption[];
  job_ids: FilterOption[];
}

export interface CronJobItem {
  id: string;
  name: string;
  tenant_id: string;
  tenant_name: string;
  bbk_id: string;
  source_id: string;
  enabled: boolean;
  task_type: string;
  cron_expr: string;
  timezone: string;
  channel: string;
  target_user_id: string;
  target_session_id: string;
  timeout_seconds: number;
  max_concurrency: number;
  misfire_grace_seconds: number;
  text_content: string;
  request_input: string;
  creator_user_id: string;
  task_chat_id: string;
  task_session_id: string;
  job_origin: string;
  subscription_key: string;
  meta: string;
  status: string;
  pause_reason: string;
  execution_count: number;
  today_status: string | null; // 今日最新执行状态: success/running/error/cancelled/timeout/skipped
  created_at: string | null;
  updated_at: string | null;
  deleted_at: string | null;
}

export interface ExecutionItem {
  id: number;
  job_id: string;
  job_name: string;
  tenant_id: string;
  tenant_name: string;
  bbk_id?: string;
  scheduled_time: string | null;
  actual_time: string;
  end_time: string | null;
  duration_ms: number;
  status: string;
  async_status?: string | null;
  error_message: string;
  instance_id: string;
  executor_leader: string;
  is_manual: boolean;
  trace_id: string;
  session_id: string;
  input_snapshot: string;
  output_preview: string;
  meta: string;
  is_read: boolean;
  read_at: string | null;
  created_at: string | null;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface MarkReadResponse {
  marked: boolean;
  count: number;
}

export interface UnreadCountItem {
  job_id: string;
  job_name: string;
  unread_count: number;
}

export interface UnreadCountResponse {
  items: UnreadCountItem[];
  total_unread: number;
}

export interface CronOverviewMetricItem {
  key: string;
  value: number;
  compare: string;
  trend: "up" | "down" | null;
}

export interface CronOverviewDistributionItem {
  name: string;
  value: number;
  percent: number;
}

export interface CronOverviewBranchExecutionItem {
  name: string;
  success: number;
  failed: number;
  skipped: number;
}

export interface CronOverviewBranchReadItem {
  name: string;
  read: number;
  unread: number;
}

export interface CronOverviewResponse {
  start_time: string | null;
  end_time: string | null;
  metrics: CronOverviewMetricItem[];
  task_status: CronOverviewDistributionItem[];
  execution_result: CronOverviewDistributionItem[];
  read_status: CronOverviewDistributionItem[];
  failure_reasons: CronOverviewDistributionItem[];
  branch_tasks: CronOverviewDistributionItem[];
  branch_execution: CronOverviewBranchExecutionItem[];
  branch_read: CronOverviewBranchReadItem[];
}

export type CronScheduleBucketMinutes = 5 | 10 | 15 | 30 | 60;
export type CronScheduleTaskType = "text" | "agent";

export interface CronScheduleDistributionDiagnostics {
  invalid_cron_jobs: number;
  invalid_timezone_jobs: number;
  unsupported_task_type_jobs: number;
  invalid_metadata_jobs: number;
  managed_child_jobs: number;
}

export interface CronScheduleDistributionBucket {
  start_time: string;
  end_time: string;
  text_count: number;
  agent_count: number;
  total_count: number;
}

export interface CronScheduleDistributionResponse {
  start_time: string;
  end_time: string;
  bucket_minutes: CronScheduleBucketMinutes;
  calculated_at: string;
  definition_revision: string;
  eligible_job_count: number;
  text_count: number;
  agent_count: number;
  total_count: number;
  buckets: CronScheduleDistributionBucket[];
  diagnostics: CronScheduleDistributionDiagnostics;
}

export interface CronScheduleOccurrenceItem {
  scheduled_at: string;
  job_id: string;
  job_name: string;
  user_name: string;
  user_id: string;
  task_type: CronScheduleTaskType;
  cron_expr: string;
  timezone: string;
}

export interface CronScheduleDistributionDetailsResponse {
  start_time: string;
  end_time: string;
  task_type: CronScheduleTaskType | null;
  calculated_at: string;
  definition_revision: string;
  items: CronScheduleOccurrenceItem[];
  total: number;
  page: number;
  page_size: number;
  diagnostics: CronScheduleDistributionDiagnostics;
}

export interface CronScheduleDistributionParams {
  start_time: string;
  end_time: string;
  bucket_minutes: CronScheduleBucketMinutes;
}

export interface CronScheduleDistributionDetailsParams {
  start_time: string;
  end_time: string;
  task_type?: CronScheduleTaskType;
  page?: number;
  page_size?: number;
  definition_revision?: string;
}

export interface CronDispatchBatchStats {
  total_batches: number;
  running_batches: number;
  completed_batches: number;
  failed_batches: number;
  total_intents: number;
  completed_intents: number;
  failed_intents: number;
  pending_intents: number;
}

export interface CronDispatchBatchItem {
  batch_id: string;
  parent_job_id: string;
  parent_job_name: string;
  parent_external_job_id: string;
  tenant_id: string;
  source_id: string;
  provider_id: string;
  model_id: string;
  agent_id: string;
  scheduled_fire_at: string | null;
  callback_received_at: string | null;
  status: string;
  lock_owner: string;
  locked_at: string | null;
  total_count: number;
  completed_count: number;
  failed_count: number;
  error_message: string;
  completed_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CronDispatchIntentItem {
  id: number;
  batch_id: string;
  intent_role: string;
  status: string;
  source_id: string;
  provider_id: string;
  model_id: string;
  tenant_id: string;
  agent_id: string;
  job_id: string;
  parent_job_id: string;
  scheduled_fire_at: string | null;
  due_at: string | null;
  dispatch_order: number;
  viewer_heat_score: number;
  attempt_count: number;
  max_attempts: number;
  lock_owner: string;
  locked_at: string | null;
  acked_at: string | null;
  completed_at: string | null;
  error_message: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface CronDispatchEventItem {
  id: number;
  batch_id: string;
  intent_id: number | null;
  event_type: string;
  worker_id: string;
  job_id: string;
  tenant_id: string;
  source_id: string;
  details: Record<string, unknown> | null;
  created_at: string | null;
}

export interface CronDispatchBatchesResponse {
  source_id: string;
  start_time: string | null;
  end_time: string | null;
  stats: CronDispatchBatchStats;
  items: CronDispatchBatchItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface CronDispatchBatchDetailResponse {
  batch: CronDispatchBatchItem;
  intents: CronDispatchIntentItem[];
  intent_total: number;
  intent_filtered_total: number;
  intent_page: number;
  intent_page_size: number;
  events: CronDispatchEventItem[];
  event_total: number;
  event_page: number;
  event_page_size: number;
}

export interface CronDispatchPolicyItem {
  source_id: string;
  provider_id: string;
  model_id: string;
  default_strategy_id: string;
  strategy_schedule: Array<Record<string, unknown>> | null;
  enabled: boolean;
  strategy: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CronDispatchCapacityItem {
  id: number;
  worker_id: string;
  source_id: string;
  provider_id: string;
  model_id: string;
  strategy_id: string;
  previous_workers: number;
  baseline_workers: number;
  min_workers: number;
  max_workers: number;
  effective_workers: number;
  pending_count: number;
  claimed_count: number;
  running_count: number;
  success_count: number;
  failure_count: number;
  error_rate: number;
  matched_rule: Record<string, unknown> | null;
  avg_latency_ms: number;
  decision_reason: string;
  created_at: string | null;
}

export interface CronDispatchWorkersResponse {
  source_id: string;
  policies: CronDispatchPolicyItem[];
  current_capacity: CronDispatchCapacityItem[];
  capacity_events: CronDispatchCapacityItem[];
}

export interface AsyncTaskItemRecord {
  task_id: string;
  target_id: string;
  target_name?: string | null;
  status: string;
  error_message?: string | null;
  result_json?: unknown;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AsyncTaskRecord {
  task_id: string;
  service: string;
  task_type: string;
  status: string;
  title: string;
  summary?: string | null;
  source_id?: string | null;
  actor_user_id?: string | null;
  actor_user_name?: string | null;
  target_count: number;
  done_count: number;
  failed_count: number;
  error_message?: string | null;
  result_json?: unknown;
  created_at?: string | null;
  updated_at?: string | null;
  finished_at?: string | null;
}

export interface AsyncTaskDetailRecord extends AsyncTaskRecord {
  items: AsyncTaskItemRecord[];
}

export interface AsyncTaskListResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface AsyncTaskQueryFilters {
  source_id?: string;
  task_type?: string;
  status?: string;
  keyword?: string;
  page?: number;
  page_size?: number;
}

export interface HighFrequencyQuestionCriteria {
  start_time: string;
  end_time: string;
  bbk_id?: string | null;
  force?: boolean;
}

export interface HighFrequencyQuestionTopic {
  rank_no: number;
  topic_name: string;
  message_count: number;
  valid_message_count: number;
  bbk_dis?: Record<string, number>;
  sample_questions: string[];
}

export interface HighFrequencyQuestionResult {
  state: "AVAILABLE" | "AVAILABLE_STALE" | "EMPTY";
  task_id?: string | null;
  batch_id?: string | null;
  status?: string | null;
  source_id: string;
  stat_start_time?: string | null;
  stat_end_time?: string | null;
  scope_type?: "ALL" | "ORG" | null;
  bbk_id?: string | null;
  result_updated_at?: string | null;
  topics: HighFrequencyQuestionTopic[];
  message?: string | null;
}

export interface HighFrequencyQuestionTaskResult
  extends Omit<HighFrequencyQuestionResult, "state"> {
  state: "AVAILABLE" | "RUNNING";
}

export interface CronDispatchDateFilters {
  start_time?: string;
  end_time?: string;
  status?: string;
  query?: string;
}

export interface CronJobOverviewSummaryMetric {
  key: string;
  value: string;
  hintValue?: string;
  footerValue?: string;
}

export interface CronJobOverviewBranchRankingRow {
  rank: number | "...";
  bbkId: string;
  branchName: string;
  skillCount: string;
  totalTasks: string;
  successCount: string;
  readTasks: string;
  involvedManagers: string;
  resultViewManagers: string;
  resultViewManagerRate: string;
  planManagers: string;
  planManagerRate: string;
  insightManagers: string;
  insightManagerRate: string;
  phoneManagers: string;
  phoneManagerRate: string;
  recommendedCustomers: string;
  viewedCustomers: string;
  viewedCustomerRate: string;
  contactedCustomers: string;
  contactRate: string;
  insightCustomers: string;
  phoneCustomers: string;
}

export interface CronBranchTaskRankingItem {
  bbk_id: string;
  bbk_name: string;
  manager_count: number;
  total_tasks: number;
  success_count: number;
  success_rate: number;
  read_tasks: number;
  plan_count: number;
  insight_count: number;
  phone_count: number;
  plan_clicks: number;
  insight_clicks: number;
  phone_clicks: number;
  error_count: number;
}

export interface CronBranchTaskRankingResponse {
  start_date: string;
  end_date: string;
  items: CronBranchTaskRankingItem[];
}

export interface CronJobOverviewFailureReason {
  name: string;
  count: number;
  percent: number;
  color: string;
}

export interface CronJobOverviewAnomalySummary {
  affectedBranches: string;
  affectedBranchesUnit: string;
  affectedManagers: string;
  affectedManagersUnit: string;
}

export interface CronJobOverviewAnomalyRankRow {
  rank: number;
  branchName: string;
  alertExecutions: string;
  alertRate: string;
  affectedManagers: string;
  latestAlertTime: string;
}

export interface CronJobOverviewPageData {
  summaryMetrics: CronJobOverviewSummaryMetric[];
  branchRankingRows: CronJobOverviewBranchRankingRow[];
  failureReasons: CronJobOverviewFailureReason[];
  anomalySummary: CronJobOverviewAnomalySummary;
  anomalyRankRows: CronJobOverviewAnomalyRankRow[];
}

export interface CronJobOverviewDateFilters {
  start_date?: string;
  end_date?: string;
  bbk_ids?: string;
}

export type CronBranchDimensionSortKey = Exclude<
  keyof CronJobOverviewBranchRankingRow,
  "rank" | "bbkId" | "branchName"
>;

export type CronBranchDimensionExportFilters = {
  start_date: string;
  end_date: string;
  bbk_ids?: string;
} & (
  | { sort_by: CronBranchDimensionSortKey; sort_order: "asc" | "desc" }
  | { sort_by?: never; sort_order?: never }
);

export interface CronOverviewStatsResponse {
  start_date: string;
  end_date: string;
  total_tasks: number;
  new_cron_tasks: number;
  total_executions: number;
  branch_count: number;
  tenant_count: number;
  success_rate: number;
  success_count: number;
  running_count: number;
  read_tasks: number;
  read_rate: number;
  report_rate: number;
  report_count: number;
  insight_count: number;
  phone_count: number;
  error_count: number;
  error_rate: number;
}

export interface CronBranchRankingItem {
  bbk_id: string;
  bbk_name: string;
  skill_count: number;
  total_tasks: number;
  success_count: number;
  read_tasks: number;
  involved_managers: number;
  result_view_managers: number;
  plan_managers: number;
  insight_managers: number;
  phone_managers: number;
  recommended_customers: number;
  viewed_customers: number;
  contacted_customers: number;
  contact_rate: number;
  insight_customers: number;
  phone_customers: number;
}

export interface CronBranchRankingResponse {
  start_date: string;
  end_date: string;
  items: CronBranchRankingItem[];
}

export interface CronErrorReasonItem {
  reason: string;
  count: number;
  percent: number;
}

export interface CronBranchErrorRankItem {
  bbk_id: string;
  bbk_name: string;
  total_executions: number;
  error_count: number;
  error_rate: number;
  affected_managers: number;
}

export interface CronBranchErrorResponse {
  start_date: string;
  end_date: string;
  affected_branch_count: number;
  affected_manager_count: number;
  error_reasons: CronErrorReasonItem[];
  branch_error_rank: CronBranchErrorRankItem[];
}

export interface BranchSkillItem {
  skill_name: string;
  cron_task_count: number;
  success_count: number;
  success_rate: number;
  read_count: number;
  error_count: number;
}

export interface BranchSkillResponse {
  start_date: string;
  end_date: string;
  bbk_id: string;
  bbk_name: string;
  items: BranchSkillItem[];
}

export interface BranchManagerSummaryItem {
  user_id: string;
  user_name: string;
  skill_count: number;
  total_tasks: number;
  success_count: number;
  read_tasks: number;
  recommended_customers: number;
  viewed_customers: number;
  contacted_customers: number;
  contact_rate: number;
  insight_customers: number;
  phone_customers: number;
}

export interface BranchManagerSummaryResponse {
  start_date: string;
  end_date: string;
  bbk_id: string;
  bbk_name: string;
  items: BranchManagerSummaryItem[];
}

export interface ManagerSkillItem {
  skill_name: string;
  cron_task_count: number;
  success_count: number;
  success_rate: number;
  read_count: number;
  error_count: number;
}

export interface ManagerSkillResponse {
  start_date: string;
  end_date: string;
  bbk_id: string;
  user_id: string;
  user_name: string;
  items: ManagerSkillItem[];
}

export interface ManagerCustomerItem {
  customer_id: string;
  customer_name: string;
  clicked_plan: boolean;
  clicked_insight: boolean;
  clicked_phone: boolean;
  click_time: string | null;
}

export interface ManagerCustomerResponse {
  start_date: string;
  end_date: string;
  bbk_id: string;
  user_id: string;
  user_name: string;
  items: ManagerCustomerItem[];
}

export interface BranchSkillManagerItem {
  user_id: string;
  user_name: string;
  read_count: number;
  plan_count: number;
  insight_count: number;
  phone_count: number;
  last_click_time: string | null;
}

export interface BranchSkillManagerResponse {
  start_date: string;
  end_date: string;
  bbk_id: string;
  skill_name: string;
  items: BranchSkillManagerItem[];
}

export interface BranchSkillManagerCustomerItem {
  customer_id: string;
  customer_name: string;
  clicked_plan: boolean;
  clicked_insight: boolean;
  clicked_phone: boolean;
  click_time: string | null;
}

export interface BranchSkillManagerCustomerResponse {
  start_date: string;
  end_date: string;
  bbk_id: string;
  skill_name: string;
  user_id: string;
  items: BranchSkillManagerCustomerItem[];
}

export interface SubscriptionOverviewItem {
  subscription_key: string;
  task_name: string;
  subscriber_count: number;
  total_task_count: number;
  running_task_count: number;
  pending_task_count: number;
  executed_task_count: number;
  failed_task_count: number;
  avg_duration_ms: number;
  success_rate: number;
}

export interface SubscriptionDetailItem {
  job_id: string;
  subscriber_id: string;
  subscriber_name: string;
  bbk_id: string;
  enabled: boolean;
  execution_status: string;
  execution_time: string | null;
}

const CRON_FAILURE_REASON_COLORS = [
  "#1d6ff2",
  "#38a8f5",
  "#7a8cf0",
  "#ff821c",
  "#67cdb9",
];

function appendDefinedParams(params: URLSearchParams, filters?: object) {
  if (!filters) {
    return;
  }
  Object.entries(filters).forEach(([key, value]) => {
    if (typeof value === "string" && value !== "") {
      params.append(key, value);
    }
  });
}

function buildQuery(filters?: object) {
  const params = new URLSearchParams();
  appendDefinedParams(params, filters);
  const query = params.toString();
  return query ? `?${query}` : "";
}

function formatInteger(value: number | null | undefined) {
  return Math.round(Number(value || 0)).toLocaleString("en-US");
}

function formatPercentValue(value: number | null | undefined) {
  return Number(value || 0).toFixed(2);
}

function formatPercentText(value: number | null | undefined) {
  return `${formatPercentValue(value)}%`;
}

function formatRatioPercentText(value: number | null | undefined) {
  return `${(Number(value ?? 0) * 100).toFixed(2)}%`;
}

function formatDivisionPercentText(
  numerator: number | null | undefined,
  denominator: number | null | undefined,
) {
  const denominatorValue = Number(denominator ?? 0);
  if (!Number.isFinite(denominatorValue) || denominatorValue <= 0) {
    return "0.00%";
  }
  const numeratorValue = Number(numerator ?? 0);
  return `${((numeratorValue / denominatorValue) * 100).toFixed(2)}%`;
}

export function mapCronJobOverviewPageData(
  stats: CronOverviewStatsResponse,
  behavior: CronBranchRankingResponse,
  branchError: CronBranchErrorResponse,
): CronJobOverviewPageData {
  return {
    summaryMetrics: [
      { key: "branches", value: formatInteger(stats.branch_count) },
      { key: "managers", value: formatInteger(stats.tenant_count) },
      {
        key: "tasks",
        value: formatInteger(stats.total_tasks),
        hintValue: `新增 ${formatInteger(stats.new_cron_tasks)}`,
        footerValue: `${formatInteger(stats.total_executions)} 次`,
      },
      {
        key: "success",
        value: formatPercentValue(stats.success_rate),
        footerValue: `${formatInteger(stats.success_count)}/${formatInteger(
          stats.error_count,
        )}`,
      },
      {
        key: "read",
        value: formatPercentValue(stats.read_rate),
        footerValue: formatInteger(stats.read_tasks),
      },
      {
        key: "report",
        value: formatPercentValue(stats.report_rate),
      },
      { key: "report_count", value: formatInteger(stats.report_count) },
      { key: "insight_count", value: formatInteger(stats.insight_count) },
      { key: "phone_count", value: formatInteger(stats.phone_count) },
    ],
    branchRankingRows: behavior.items.map((item, index) => ({
      rank: index + 1,
      bbkId: item.bbk_id || "",
      branchName: item.bbk_name || item.bbk_id || "-",
      skillCount: formatInteger(item.skill_count),
      totalTasks: formatInteger(item.total_tasks),
      successCount: formatInteger(item.success_count),
      readTasks: formatInteger(item.read_tasks),
      involvedManagers: formatInteger(item.involved_managers),
      resultViewManagers: formatInteger(item.result_view_managers),
      resultViewManagerRate: formatDivisionPercentText(
        item.result_view_managers,
        item.involved_managers,
      ),
      planManagers: formatInteger(item.plan_managers),
      planManagerRate: formatDivisionPercentText(
        item.plan_managers,
        item.result_view_managers,
      ),
      insightManagers: formatInteger(item.insight_managers),
      insightManagerRate: formatDivisionPercentText(
        item.insight_managers,
        item.plan_managers,
      ),
      phoneManagers: formatInteger(item.phone_managers),
      phoneManagerRate: formatDivisionPercentText(
        item.phone_managers,
        item.plan_managers,
      ),
      recommendedCustomers: formatInteger(item.recommended_customers),
      viewedCustomers: formatInteger(item.viewed_customers),
      viewedCustomerRate: formatDivisionPercentText(
        item.viewed_customers,
        item.recommended_customers,
      ),
      contactedCustomers: formatInteger(item.contacted_customers ?? 0),
      contactRate: formatRatioPercentText(item.contact_rate ?? 0),
      insightCustomers: formatInteger(item.insight_customers),
      phoneCustomers: formatInteger(item.phone_customers),
    })),
    failureReasons: branchError.error_reasons.map((item, index) => ({
      name: item.reason || "其他",
      count: Number(item.count || 0),
      percent: Number(item.percent || 0),
      color:
        CRON_FAILURE_REASON_COLORS[index % CRON_FAILURE_REASON_COLORS.length],
    })),
    anomalySummary: {
      affectedBranches: formatInteger(branchError.affected_branch_count),
      affectedBranchesUnit: "家",
      affectedManagers: formatInteger(branchError.affected_manager_count),
      affectedManagersUnit: "人",
    },
    anomalyRankRows: branchError.branch_error_rank.map((item, index) => ({
      rank: index + 1,
      branchName: item.bbk_name || item.bbk_id || "-",
      alertExecutions: formatInteger(item.error_count),
      alertRate: formatPercentText(item.error_rate),
      affectedManagers: formatInteger(item.affected_managers),
      latestAlertTime: "",
    })),
  };
}

// API functions
export const monitorApi = {
  // Get filter options for dropdown selects
  getFilterOptions: async (): Promise<FilterOptionsResponse> => {
    return request(`/monitor/cron/filter-options`);
  },

  // Get page-shaped aggregate data for the cron overview
  getCronOverview: async (filters?: {
    tenant_id?: string;
    bbk_id?: string;
    start_time?: string;
    end_time?: string;
  }): Promise<CronOverviewResponse> => {
    const params = new URLSearchParams();
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "") {
          params.append(key, value);
        }
      });
    }
    const query = params.toString();
    return request(`/monitor/cron/overview${query ? `?${query}` : ""}`);
  },

  getScheduleDistribution: async (
    params: CronScheduleDistributionParams,
  ): Promise<CronScheduleDistributionResponse> => {
    const query = new URLSearchParams();
    query.set("start_time", params.start_time);
    query.set("end_time", params.end_time);
    query.set("bucket_minutes", String(params.bucket_minutes));
    return request(`/monitor/cron/schedule-distribution?${query.toString()}`);
  },

  getScheduleDistributionDetails: async (
    params: CronScheduleDistributionDetailsParams,
  ): Promise<CronScheduleDistributionDetailsResponse> => {
    const query = new URLSearchParams();
    query.set("start_time", params.start_time);
    query.set("end_time", params.end_time);
    query.set("page", String(params.page ?? 1));
    query.set("page_size", String(params.page_size ?? 20));
    if (params.task_type) {
      query.set("task_type", params.task_type);
    }
    if (params.definition_revision) {
      query.set("expected_revision", params.definition_revision);
    }
    return request(
      `/monitor/cron/schedule-distribution/details?${query.toString()}`,
    );
  },

  getCronDispatchBatches: async (
    page = 1,
    pageSize = 20,
    filters?: CronDispatchDateFilters,
  ): Promise<CronDispatchBatchesResponse> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    appendDefinedParams(params, filters);
    const query = params.toString();
    return request(`/monitor/cron/dispatch/batches?${query}`);
  },

  getCronDispatchBatchDetail: async (
    batchId: string,
    filters?: {
      intent_page?: string;
      intent_limit?: string;
      intent_query?: string;
      intent_role?: string;
      intent_status?: string;
      event_page?: string;
      event_limit?: string;
    },
  ): Promise<CronDispatchBatchDetailResponse> => {
    return request(
      `/monitor/cron/dispatch/batches/${encodeURIComponent(
        batchId,
      )}${buildQuery(filters)}`,
    );
  },

  getCronDispatchWorkers: async (
    filters?: Omit<CronDispatchDateFilters, "status">,
  ): Promise<CronDispatchWorkersResponse> => {
    return request(`/monitor/cron/dispatch/workers${buildQuery(filters)}`);
  },

  getCronOverviewStats: async (
    filters?: CronJobOverviewDateFilters,
  ): Promise<CronOverviewStatsResponse> => {
    return request(`/monitor/cron/overview-stats${buildQuery(filters)}`);
  },

  getCronBranchRanking: async (
    filters?: CronJobOverviewDateFilters,
  ): Promise<CronBranchRankingResponse> => {
    return request(`/monitor/cron/branch-behavior${buildQuery(filters)}`);
  },

  getCronBranchTaskBehavior: async (
    filters?: CronJobOverviewDateFilters,
  ): Promise<CronBranchTaskRankingResponse> => {
    return request(`/monitor/cron/branch-task-behavior${buildQuery(filters)}`);
  },

  getCronBranchError: async (
    filters?: CronJobOverviewDateFilters,
  ): Promise<CronBranchErrorResponse> => {
    return request(`/monitor/cron/branch-error${buildQuery(filters)}`);
  },

  getBranchSkills: async (params: {
    bbk_id: string;
    start_date?: string;
    end_date?: string;
  }): Promise<BranchSkillResponse> => {
    return request(`/monitor/cron/branch-skills${buildQuery(params)}`);
  },

  getBranchManagerSummary: async (params: {
    bbk_id: string;
    start_date?: string;
    end_date?: string;
  }): Promise<BranchManagerSummaryResponse> => {
    return request(`/monitor/cron/branch-manager-summary${buildQuery(params)}`);
  },

  getManagerSkills: async (params: {
    bbk_id: string;
    user_id: string;
    start_date?: string;
    end_date?: string;
  }): Promise<ManagerSkillResponse> => {
    return request(`/monitor/cron/manager-skills${buildQuery(params)}`);
  },

  getManagerCustomers: async (params: {
    bbk_id: string;
    user_id: string;
    skill_name?: string;
    start_date?: string;
    end_date?: string;
  }): Promise<ManagerCustomerResponse> => {
    return request(`/monitor/cron/manager-customers${buildQuery(params)}`);
  },

  getBranchSkillManagers: async (params: {
    bbk_id: string;
    skill_name: string;
    start_date?: string;
    end_date?: string;
  }): Promise<BranchSkillManagerResponse> => {
    return request(`/monitor/cron/branch-skill-managers${buildQuery(params)}`);
  },

  getBranchSkillManagerCustomers: async (params: {
    bbk_id: string;
    skill_name: string;
    user_id: string;
    start_date?: string;
    end_date?: string;
  }): Promise<BranchSkillManagerCustomerResponse> => {
    return request(
      `/monitor/cron/branch-skill-manager-customers${buildQuery(params)}`,
    );
  },

  getCronJobOverviewPageData: async (
    filters?: CronJobOverviewDateFilters,
  ): Promise<CronJobOverviewPageData> => {
    const [stats, ranking, branchError] = await Promise.all([
      monitorApi.getCronOverviewStats(filters),
      monitorApi.getCronBranchRanking(filters),
      monitorApi.getCronBranchError(filters),
    ]);
    return mapCronJobOverviewPageData(stats, ranking, branchError);
  },

  // Get cron jobs list
  getJobs: async (
    page = 1,
    pageSize = 20,
    filters?: {
      tenant_id?: string;
      bbk_id?: string;
      creator_user_id?: string;
      job_origin?: string;
      status?: string;
      enabled?: boolean;
    },
  ): Promise<PaginatedResponse<CronJobItem>> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (
          value !== undefined &&
          value !== null &&
          value !== "" &&
          value !== "all"
        ) {
          params.append(key, value.toString());
        }
      });
    }
    return request(`/monitor/cron/jobs?${params.toString()}`);
  },

  // Get single job
  getJob: async (jobId: string): Promise<CronJobItem> => {
    return request(`/monitor/cron/jobs/${jobId}`);
  },

  // Get executions list
  getExecutions: async (
    page = 1,
    pageSize = 20,
    filters?: {
      job_id?: string;
      tenant_id?: string;
      bbk_id?: string;
      status?: string;
      start_time?: string;
      end_time?: string;
    },
  ): Promise<PaginatedResponse<ExecutionItem>> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "") {
          params.append(key, value);
        }
      });
    }
    return request(`/monitor/cron/executions?${params.toString()}`);
  },

  // Get single execution
  getExecution: async (executionId: number): Promise<ExecutionItem> => {
    return request(`/monitor/cron/executions/${executionId}`);
  },

  // Get subscription-level overview rows
  getSubscriptionOverview: async (
    page = 1,
    pageSize = 20,
    filters?: {
      keyword?: string;
      tenant_id?: string;
      bbk_id?: string;
      source_id?: string;
      start_time?: string;
      end_time?: string;
    },
  ): Promise<PaginatedResponse<SubscriptionOverviewItem>> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "") {
          params.append(key, value);
        }
      });
    }
    return request(`/monitor/cron/subscription-overview?${params.toString()}`);
  },

  // Get subscription detail rows for a drawer/table
  getSubscriptionDetails: async (
    subscriptionKey: string,
    page = 1,
    pageSize = 20,
    filters?: {
      tenant_id?: string;
      bbk_id?: string;
      source_id?: string;
      start_time?: string;
      end_time?: string;
    },
  ): Promise<PaginatedResponse<SubscriptionDetailItem>> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "") {
          params.append(key, value);
        }
      });
    }
    return request(
      `/monitor/cron/subscription-overview/${encodeURIComponent(
        subscriptionKey,
      )}/jobs?${params.toString()}`,
    );
  },

  // Export jobs to Excel
  exportJobs: async (filters?: {
    tenant_id?: string;
    bbk_id?: string;
    enabled?: boolean;
  }): Promise<Blob> => {
    const params = new URLSearchParams();
    params.append("export_type", "jobs");
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null)
          params.append(key, value.toString());
      });
    }
    const url = getApiUrl(`/monitor/cron/export?${params.toString()}`);
    const headers = new Headers(buildAuthHeaders());
    const response = await fetch(url, { headers });
    if (!response.ok) {
      let errorMessage = `Export failed: ${response.status} ${response.statusText}`;
      try {
        const errorData = await response.json();
        if (errorData.detail) {
          errorMessage = errorData.detail;
        }
      } catch {
        // Ignore JSON parse error
      }
      throw new Error(errorMessage);
    }
    return response.blob();
  },

  // Export executions to Excel
  exportExecutions: async (filters?: {
    job_id?: string;
    tenant_id?: string;
    status?: string;
    start_time?: string;
    end_time?: string;
  }): Promise<Blob> => {
    const params = new URLSearchParams();
    params.append("export_type", "executions");
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.append(key, value);
      });
    }
    const url = getApiUrl(`/monitor/cron/export?${params.toString()}`);
    const headers = new Headers(buildAuthHeaders());
    const response = await fetch(url, { headers });
    if (!response.ok) {
      let errorMessage = `Export failed: ${response.status} ${response.statusText}`;
      try {
        const errorData = await response.json();
        if (errorData.detail) {
          errorMessage = errorData.detail;
        }
      } catch {
        // Ignore JSON parse error
      }
      throw new Error(errorMessage);
    }
    return response.blob();
  },

  exportBranchDimension: async (
    filters: CronBranchDimensionExportFilters,
  ): Promise<Blob> => {
    const response = await fetch(
      getApiUrl(`/monitor/cron/export-branch-dimension${buildQuery(filters)}`),
      { headers: new Headers(buildAuthHeaders()) },
    );
    if (!response.ok) {
      const errorData = await response.json().catch(() => null);
      throw new Error(
        typeof errorData?.detail === "string"
          ? errorData.detail
          : `导出失败（HTTP ${response.status}），请稍后重试`,
      );
    }
    if (
      !response.headers
        .get("content-type")
        ?.includes(
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    ) {
      throw new Error("导出接口未返回 Excel 文件，请稍后重试");
    }
    return response.blob();
  },

  // Export overview execution/customer detail to Excel
  exportSkillUsageDetails: async (filters?: {
    start_date?: string;
    end_date?: string;
    bbk_ids?: string;
  }): Promise<Blob> => {
    const params = new URLSearchParams();
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "") {
          params.append(key, value);
        }
      });
    }
    const query = params.toString();
    const url = getApiUrl(
      `/monitor/cron/export-detail${query ? `?${query}` : ""}`,
    );
    const headers = new Headers(buildAuthHeaders());
    const response = await fetch(url, { headers });
    if (!response.ok) {
      let errorMessage = `Export failed: ${response.status} ${response.statusText}`;
      try {
        const errorData = await response.json();
        if (errorData.detail) {
          errorMessage = errorData.detail;
        }
      } catch {
        // Ignore JSON parse error
      }
      throw new Error(errorMessage);
    }
    return response.blob();
  },

  // Mark job as read
  markJobAsRead: async (jobId: string): Promise<MarkReadResponse> => {
    return request(`/monitor/cron/jobs/${jobId}/mark-read`, { method: "POST" });
  },

  // Get unread count
  getUnreadCount: async (tenantId?: string): Promise<UnreadCountResponse> => {
    const params = new URLSearchParams();
    if (tenantId) {
      params.append("tenant_id", tenantId);
    }
    return request(`/monitor/cron/unread-count?${params.toString()}`);
  },

  getAsyncTasks: async (
    filters?: AsyncTaskQueryFilters,
  ): Promise<AsyncTaskListResponse<AsyncTaskRecord>> => {
    const params = new URLSearchParams();
    const page = filters?.page ?? 1;
    const pageSize = filters?.page_size ?? 20;
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (
          key === "page" ||
          key === "page_size" ||
          value === undefined ||
          value === null ||
          value === ""
        ) {
          return;
        }
        params.append(key, String(value));
      });
    }
    return request(`/monitor/tasks?${params.toString()}`);
  },

  getAsyncTaskDetail: async (
    taskId: string,
    sourceId?: string,
  ): Promise<AsyncTaskDetailRecord> => {
    const params = new URLSearchParams();
    if (sourceId) {
      params.append("source_id", sourceId);
    }
    const query = params.toString() ? `?${params.toString()}` : "";
    return request(`/monitor/tasks/${encodeURIComponent(taskId)}${query}`);
  },

  getHighFrequencyQuestionResults: async (
    criteria: HighFrequencyQuestionCriteria,
  ): Promise<HighFrequencyQuestionResult> => {
    const params = new URLSearchParams();
    params.append("start_time", criteria.start_time);
    params.append("end_time", criteria.end_time);
    if (criteria.bbk_id) {
      params.append("bbk_id", criteria.bbk_id);
    }
    return request(
      `/monitor/high-frequency-question/results?${params.toString()}`,
    );
  },

  submitHighFrequencyQuestionTask: async (
    criteria: HighFrequencyQuestionCriteria,
  ): Promise<HighFrequencyQuestionTaskResult> => {
    return request("/monitor/high-frequency-question/tasks", {
      method: "POST",
      body: JSON.stringify(criteria),
    });
  },
};
