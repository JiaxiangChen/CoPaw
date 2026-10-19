import type { CronJobSpecOutput } from "@/api/types";
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

/** 只适配新版编辑器，不改变旧页面的表单转换行为。 */
export function buildManagedCronFormValues(job: CronJobSpecOutput) {
  const values = buildCronJobFormValues(job);
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
  values: CronJobFormValues,
  original?: CronJobSpecOutput | null,
) {
  const payload = buildCronJobSubmitPayload(values);
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
