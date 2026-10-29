import { request } from "../request";
import type {
  TaskIdentity,
  TrackingFilters,
  TrackingLookup,
  TrackingOverview,
  TrackingTimeline,
} from "../types/cronTaskTracking";

function read<T>(
  source: string,
  job: string,
  action: string,
  filters: object,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  const query = params.toString() ? `?${params}` : "";
  return request<T>(
    `/scheduler/cron/task-tracking/jobs/${encodeURIComponent(
      job,
    )}/${action}${query}`,
    {
      headers: { "X-Source-Id": source },
      signal,
    },
  );
}
const selected = (identity: TaskIdentity) => ({
  tenant_id: identity.tenant_id,
  agent_id: identity.agent_id,
});

export const cronTaskTracking = {
  lookup: (
    source: string,
    job: string,
    filters: { tenant_id?: string; agent_id?: string } = {},
    signal?: AbortSignal,
  ) => read<TrackingLookup>(source, job, "lookup", filters, signal),
  overview: (source: string, identity: TaskIdentity, signal?: AbortSignal) =>
    read<TrackingOverview>(
      source,
      identity.job_id,
      "overview",
      selected(identity),
      signal,
    ),
  timeline: (
    source: string,
    identity: TaskIdentity,
    filters: TrackingFilters,
    signal?: AbortSignal,
  ) =>
    read<TrackingTimeline>(
      source,
      identity.job_id,
      "timeline",
      { ...selected(identity), ...filters },
      signal,
    ),
};
