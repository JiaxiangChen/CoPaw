export { RequestIdentity } from "@/api/requestIdentity";
import type { TFunction } from "i18next";
import { parseCron } from "@/utils/parseCron";
import type {
  ManagedChild,
  ManagedJob,
  ManagedJobDetail,
  ManagedSelectedChild,
  ManagedExecution,
  ManagedDispatchMode,
} from "@/api/types/schedulerCron";

export const MODE_LABELS = {
  ordinary: "普通调度",
  batch_enabled: "开启批调度",
  batch_paused: "暂停批调度",
} as const;

export function isBatchDispatchMode(mode: ManagedDispatchMode): boolean {
  return mode === "batch_enabled" || mode === "batch_paused";
}

export function executionNeedsPolling(
  execution: ManagedExecution | null,
): boolean {
  return Boolean(
    execution &&
      (["queued", "dispatching", "accepted", "running"].includes(
        execution.status,
      ) ||
        (execution.status === "success" && execution.settled_status === null)),
  );
}

/** 沿用原页的排程文字，不依赖旧批调度元数据。 */
export function scheduleLabel(cron: string, t: TFunction): string {
  const parts = parseCron(cron || "0 9 * * *");
  if (parts.type === "custom") return cron;
  if (parts.type === "hourly") return t("cronJobs.cronTypeHourly");
  const clock = `${String(parts.hour ?? 9).padStart(2, "0")}:${String(
    parts.minute ?? 0,
  ).padStart(2, "0")}`;
  if (parts.type === "daily") return `${t("cronJobs.cronTypeDaily")} ${clock}`;
  const days = (parts.daysOfWeek ?? [])
    .map((day) =>
      t(`cronJobs.cronDay${day.charAt(0).toUpperCase()}${day.slice(1)}`),
    )
    .join(",");
  return `${t("cronJobs.cronTypeWeekly")} ${days} ${clock}`;
}

export function detailToJob(detail: ManagedJobDetail): ManagedJob {
  const { spec, ...status } = detail;
  return { ...spec, ...status };
}

export function selectedChildren(
  children: ManagedChild[],
): ManagedSelectedChild[] {
  return children.map((child) => ({
    job_id: child.id,
    tenant_id: child.tenant_id,
    agent_id: child.agent_id,
    expected_definition_version: child.definition_version,
  }));
}

export function failureMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "请求失败，请刷新核对进度后重试";
}
