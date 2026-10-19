import dayjs from "dayjs";
import type {
  CronBroadcastTaskResponse,
  CronBroadcastTenantResult,
  CronJobSpecInput,
  CronJobSpecOutput,
} from "@/api/types";
import {
  buildExecutionModelKey,
  parseExecutionModelKey,
} from "@/hooks/useExecutionModelOptions";
import {
  getNotificationDelayFormValue,
  toNotificationDelayMinutes,
  type NotificationDelayUnit,
} from "@/utils/cron";
import type { CronParts } from "./components/parseCron";
import {
  parseCron,
  serializeCron,
  CronType,
} from "./components/parseCron";


const MAX_SKILL_IDS_LENGTH = 200;

export type SkillSelectOption = { value: string; label: string };

export type CronJobFormValues = CronJobSpecOutput & {
  cronType?: string;
  cronTime?: dayjs.Dayjs;
  cronDaysOfWeek?: string[];
  cronCustom?: string;
  skillIds?: string | string[];
  execution_model_key?: string;
  notificationDelayValue?: number;
  notificationDelayUnit?: NotificationDelayUnit;
};

export function buildSkillSelectOptions(
  skills: Array<{
    skill_id: string;
    skill_name: string;
    cn_name?: string | null;
  }>,
): SkillSelectOption[] {
  const seenSkillIds = new Set<string>();
  return skills.reduce<SkillSelectOption[]>((options, skill) => {
    const skillId = skill.skill_id.trim();
    if (!skillId || seenSkillIds.has(skillId)) {
      return options;
    }
    seenSkillIds.add(skillId);
    const displayName = skill.cn_name || skill.skill_name || skillId;
    options.push({
      value: skillId,
      label: displayName === skillId ? skillId : `${displayName} (${skillId})`,
    });
    return options;
  }, []);
}

export function normalizeSkillIdsInput(value?: unknown): string | undefined {
  const rawSkillIds = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.trim().split(/[,\s]+/)
      : [];

  if (!rawSkillIds.length) {
    return undefined;
  }

  const skillIds = Array.from(
    new Set(
      rawSkillIds
        .map((skillId) => (typeof skillId === "string" ? skillId.trim() : ""))
        .filter(Boolean),
    ),
  );

  const normalized = skillIds.join(",");
  if (!normalized) {
    return undefined;
  }
  if (normalized.length > MAX_SKILL_IDS_LENGTH) {
    throw new Error("绑定技能ID总长度不能超过200个字符");
  }
  return normalized;
}

export function buildCronJobFormValues(
  job: CronJobSpecOutput,
): CronJobFormValues {
  const cronParts = parseCron(job.schedule?.cron || "0 9 * * *");
  const notificationDelay = getNotificationDelayFormValue(
    job.meta?.notification_delay_minutes,
  );
  const meta = {
    ...(job.meta || {}),
  };
  const formValues: CronJobFormValues = {
    ...job,
    meta,
    request: {
      ...job.request,
      input: job.request?.input
        ? JSON.stringify(job.request.input, null, 2)
        : "",
    },
    cronType: cronParts.type,
    skillIds: job.skill_ids
      ? job.skill_ids
        .split(",")
        .map((skillId) => skillId.trim())
        .filter(Boolean)
      : [],
    execution_model_key: buildExecutionModelKey(job.model_slot),
    notificationDelayValue: notificationDelay.value,
    notificationDelayUnit: notificationDelay.unit,
  };


  if (cronParts.type === CronType.daily || cronParts.type === CronType.weekly) {
    formValues.cronTime = dayjs()
      .hour(cronParts.hour ?? 9)
      .minute(cronParts.minute ?? 0);
  }
  if (cronParts.type === CronType.weekly && cronParts.daysOfWeek) {
    formValues.cronDaysOfWeek = cronParts.daysOfWeek;
  }
  if (cronParts.type === CronType.custom && cronParts.rawCron) {
    formValues.cronCustom = cronParts.rawCron;
  }
  return formValues;
}

export function buildCronJobSubmitPayload(
  values: CronJobFormValues,
): CronJobSpecInput {
  const cronParts: CronParts = {
    type: values.cronType || "daily",
  } as CronParts;
  if (values.cronType === "daily" || values.cronType === "weekly") {
    if (values.cronTime) {
      cronParts.hour = values.cronTime.hour();
      cronParts.minute = values.cronTime.minute();
    }
  }
  if (values.cronType === "weekly" && values.cronDaysOfWeek) {
    cronParts.daysOfWeek = values.cronDaysOfWeek;
  }
  if (values.cronType === "custom" && values.cronCustom) {
    cronParts.rawCron = values.cronCustom;
  }

  const cronExpression = serializeCron(cronParts);
  const {
    execution_model_key: executionModelKey,
    notificationDelayValue,
    notificationDelayUnit,
    skillIds,
    skill_ids: existingSkillIds,
    ...rawValues
  } = values;
  const normalizedSkillIds = normalizeSkillIdsInput(
    skillIds ?? existingSkillIds,
  );
  const notificationDelayMinutes = toNotificationDelayMinutes(
    notificationDelayValue,
    notificationDelayUnit || "minutes",
  );
  const meta: Record<string, unknown> = {
    ...(values.meta || {}),
    notification_delay_minutes: notificationDelayMinutes,
  };
  delete meta.broadcast_dispatch_intents_enabled;
  delete meta.dispatch_intents_enabled;
  let processedValues: Record<string, unknown> = {
    ...rawValues,
    schedule: {
      ...values.schedule,
      cron: cronExpression,
    },
    skill_ids: normalizedSkillIds,
    meta,
    model_slot:
      values.task_type === "agent"
        ? parseExecutionModelKey(executionModelKey)
        : undefined,
  };

  if (
    values.task_type === "agent" &&
    values.request?.input &&
    typeof values.request.input === "string"
  ) {
    processedValues = {
      ...processedValues,
      request: {
        ...values.request,
        input: JSON.parse(values.request.input),
      },
    };
  }

  if (values.task_type === "workflow") {
    delete processedValues.request;
    delete processedValues.text;
    delete processedValues.model_slot;
  } else {
    delete processedValues.workflow_binding_id;
  }

  return processedValues as unknown as CronJobSpecInput;
}

export function getBroadcastResultMessage(
  results: CronBroadcastTenantResult[],
): { tone: "success" | "warning"; text: string } {
  const successCount = results.filter((item) => item.success).length;
  const failedCount = results.length - successCount;
  const warningCount = results.filter((item) => item.warning).length;

  if (failedCount > 0) {
    return {
      tone: "warning",
      text: `Broadcasted ${successCount}, failed ${failedCount}`,
    };
  }
  if (warningCount > 0) {
    return {
      tone: "warning",
      text: `Broadcasted ${successCount} tenants, ${warningCount} with warnings`,
    };
  }
  return {
    tone: "success",
    text: `Broadcasted ${successCount} tenants`,
  };
}

export function getBroadcastTaskProgressText(
  task: CronBroadcastTaskResponse,
): string {
  const completedCount = Math.min(task.completed_count, task.tenant_count);
  const failedSuffix =
    task.failed_count > 0 ? `, failed ${task.failed_count}` : "";
  if (task.status === "running") {
    return `Broadcasting ${completedCount}/${task.tenant_count} tenants${failedSuffix}`;
  }
  if (task.status === "failed") {
    if (task.failure_summary) {
      return `Broadcast failed: ${task.failure_summary}`;
    }
    return `Broadcast finished with ${task.failed_count} failed of ${task.tenant_count} tenants`;
  }
  return `Broadcast completed ${completedCount}/${task.tenant_count} tenants${failedSuffix}`;
}
