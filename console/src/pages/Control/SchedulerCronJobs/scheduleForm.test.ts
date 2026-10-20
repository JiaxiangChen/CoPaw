import { expect, it } from "vitest";
import dayjs from "dayjs";
import type { CronJobSpecOutput } from "@/api/types";
import {
  buildManagedCronFormValues,
  buildManagedCronSubmitPayload,
} from "./scheduleForm";

function jobWithCron(cron: string): CronJobSpecOutput {
  return {
    id: "time-job",
    name: "时间测试",
    enabled: true,
    task_type: "text",
    text: "test",
    schedule: { type: "cron", cron, timezone: "Asia/Shanghai" },
    meta: {},
  } as CronJobSpecOutput;
}

it.each([
  ["0 10 * * *", "10:00"],
  ["0 0 * * *", "00:00"],
  ["7 23 * * *", "23:07"],
])("每日单时间%s正确回显%s且原样保存", (cron, time) => {
  const job = jobWithCron(cron);
  const values = buildManagedCronFormValues(job);
  expect(values.cronType).toBe("daily");
  expect(values.cronTime?.format("HH:mm")).toBe(time);
  expect(buildManagedCronSubmitPayload(values, job).schedule.cron).toBe(cron);
});

it.each([
  "0 9,10,11 * * *",
  "15 9-11 * * *",
  "*/5 * * * *",
  "* * * * *",
  "0 9 15 * *",
  "0 9 20 5 *",
  "0 9,18 * * mon",
  "0,30 9 * * *",
])("复杂排程%s使用自定义且不丢失", (cron) => {
  const job = jobWithCron(cron);
  const values = buildManagedCronFormValues(job);
  expect(values.cronType).toBe("custom");
  expect(values.cronCustom).toBe(cron);
  expect(buildManagedCronSubmitPayload(values, job).schedule.cron).toBe(cron);
});

it.each(["0 * * * *", "15 10 * * mon,wed", "0 10 * * 1,3", "00 10 * * *"])(
  "仅改名称时保留原Cron文本%s",
  (cron) => {
    const job = jobWithCron(cron);
    const values = { ...buildManagedCronFormValues(job), name: "新名称" };
    const payload = buildManagedCronSubmitPayload(values, job);
    expect(payload.name).toBe("新名称");
    expect(payload.schedule).toMatchObject({ cron, timezone: "Asia/Shanghai" });
  },
);

it("主动修改时间时提交新时间", () => {
  const job = jobWithCron("0 10 * * *");
  const values = {
    ...buildManagedCronFormValues(job),
    cronTime: dayjs().hour(11).minute(30),
  };
  expect(buildManagedCronSubmitPayload(values, job).schedule.cron).toBe(
    "30 11 * * *",
  );
});

it("主动修改自定义或星期时不恢复旧排程", () => {
  const job = jobWithCron("0 9,18 * * *");
  const custom = {
    ...buildManagedCronFormValues(job),
    cronCustom: "0 8,20 * * *",
  };
  expect(buildManagedCronSubmitPayload(custom, job).schedule.cron).toBe(
    "0 8,20 * * *",
  );
  const weekly = jobWithCron("0 10 * * mon");
  const values = {
    ...buildManagedCronFormValues(weekly),
    cronDaysOfWeek: ["fri"],
  };
  expect(buildManagedCronSubmitPayload(values, weekly).schedule.cron).toBe(
    "0 10 * * fri",
  );
});

it("新建任务不依赖原始快照", () => {
  const values = buildManagedCronFormValues(jobWithCron("0 10 * * *"));
  expect(buildManagedCronSubmitPayload(values).schedule.cron).toBe(
    "0 10 * * *",
  );
});
