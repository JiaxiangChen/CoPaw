import type { ManagedRunDateRange } from "@/api/types/schedulerCron";

/** 展示配置的日期段，不伪装成下一次运行时间或平台实际开关。 */
export function dateRangeLabel(period?: ManagedRunDateRange | null): string {
  return period ? `${period.start_date} 至 ${period.end_date}` : "不限日期段";
}

export function isDatePause(reason?: string | null): boolean {
  return Boolean(reason?.startsWith("date_"));
}
