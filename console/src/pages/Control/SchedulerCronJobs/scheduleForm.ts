import type { CronJobSpecOutput } from "@/api/types";
import dayjs, { type Dayjs } from "dayjs";
import type {
  ManagedCronSpec,
  ManagedRunDateRange,
} from "@/api/types/schedulerCron";
import {
  buildCronJobFormValues,
  buildCronJobSubmitPayload,
  type CronJobFormValues,
} from "../CronJobs/helpers";
import {
  CronType,
  parseCron,
  serializeCron,
} from "../CronJobs/components/parseCron";

export type ManagedCronFormValues = CronJobFormValues & {
  runDates?: [Dayjs, Dayjs] | null;
};
type DatedJob = CronJobSpecOutput & {
  run_date_range?: ManagedRunDateRange | null;
};

/** 只适配新版编辑器，不改变旧页面的表单转换行为。 */
export function buildManagedCronFormValues(job: DatedJob) {
  const values: ManagedCronFormValues = buildCronJobFormValues(job);
  values.runDates = job.run_date_range
    ? [dayjs(job.run_date_range.start_date), dayjs(job.run_date_range.end_date)]
    : null;
  const cron = job.schedule.cron;
  const parts = parseCron(cron);
  if (parts.type === CronType.daily && (parts.hours?.length ?? 1) === 1) {
    values.cronTime = values.cronTime?.hour(
      parts.hours?.[0] ?? parts.hour ?? 9,
    );
  } else if (parts.type !== CronType.hourly && parts.type !== CronType.weekly) {
    // 原表单没有多小时、月/年或分钟间隔控件，使用已有自定义输入。
    values.cronType = CronType.custom;
    values.cronCustom = cron;
  }
  return values;
}

export function buildManagedCronSubmitPayload(
  values: ManagedCronFormValues,
  original?: DatedJob | null,
) {
  const payload: ManagedCronSpec = buildCronJobSubmitPayload(values);
  payload.run_date_range = original?.meta?.broadcast_source_job_id
    ? original.run_date_range ?? null
    : values.runDates
    ? {
        start_date: values.runDates[0].format("YYYY-MM-DD"),
        end_date: values.runDates[1].format("YYYY-MM-DD"),
      }
    : null;
  if (
    original &&
    serializeCron(parseCron(payload.schedule.cron)) ===
      serializeCron(parseCron(original.schedule.cron))
  ) {
    // 只改名称等其他字段时，保留原Cron文本（包括数字星期等写法）。
    payload.schedule.cron = original.schedule.cron;
  }
  return payload;
}
