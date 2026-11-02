export type CustomScheduleMode =
  | "daily"
  | "weekly"
  | "minutes"
  | "hours"
  | "monthly"
  | "yearly";

export interface CustomScheduleConfig {
  mode: CustomScheduleMode;
  interval: number;
  month: number;
  dayOfMonth: number;
  hour: number;
  minute: number;
  daysOfWeek: string[];
}

export const DEFAULT_CUSTOM_SCHEDULE: CustomScheduleConfig = {
  mode: "minutes",
  interval: 30,
  month: 1,
  dayOfMonth: 1,
  hour: 8,
  minute: 0,
  daysOfWeek: ["mon"],
};

function boundedInteger(value: number, min: number, max: number): number {
  const integer = Number.isFinite(value) ? Math.trunc(value) : min;
  return Math.min(max, Math.max(min, integer));
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

export function buildCustomCron(config: CustomScheduleConfig): string {
  const hour = boundedInteger(config.hour, 0, 23);
  const minute = boundedInteger(config.minute, 0, 59);
  if (config.mode === "daily") {
    return `${minute} ${hour} * * *`;
  }
  if (config.mode === "weekly") {
    const days = config.daysOfWeek.length
      ? config.daysOfWeek.join(",")
      : DEFAULT_CUSTOM_SCHEDULE.daysOfWeek.join(",");
    return `${minute} ${hour} * * ${days}`;
  }
  if (config.mode === "minutes") {
    return `*/${boundedInteger(config.interval, 1, 59)} * * * *`;
  }
  if (config.mode === "hours") {
    return `0 */${boundedInteger(config.interval, 1, 23)} * * *`;
  }
  const day = boundedInteger(config.dayOfMonth, 1, 31);
  if (config.mode === "yearly") {
    const month = boundedInteger(config.month, 1, 12);
    return `${minute} ${hour} ${day} ${month} *`;
  }
  return `${minute} ${hour} ${day} * *`;
}

export function parseCustomSchedule(
  rawCron: string | undefined,
): CustomScheduleConfig | null {
  const fields = rawCron?.trim().split(/\s+/);
  if (!fields || fields.length !== 5) return null;

  const [minute, hour, dayOfMonth, month, dayOfWeek] = fields;
  if (
    /^\d+$/.test(minute) &&
    /^\d+$/.test(hour) &&
    dayOfMonth === "*" &&
    month === "*"
  ) {
    const parsedMinute = Number(minute);
    const parsedHour = Number(hour);
    if (parsedMinute <= 59 && parsedHour <= 23) {
      if (dayOfWeek === "*") {
        return {
          ...DEFAULT_CUSTOM_SCHEDULE,
          mode: "daily",
          hour: parsedHour,
          minute: parsedMinute,
        };
      }
      const daysOfWeek = dayOfWeek.split(",");
      if (
        daysOfWeek.length > 0 &&
        daysOfWeek.every((day) =>
          ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].includes(day),
        )
      ) {
        return {
          ...DEFAULT_CUSTOM_SCHEDULE,
          mode: "weekly",
          hour: parsedHour,
          minute: parsedMinute,
          daysOfWeek,
        };
      }
    }
  }
  const minuteInterval = /^\*\/(\d+)$/.exec(minute);
  if (
    minuteInterval &&
    hour === "*" &&
    dayOfMonth === "*" &&
    month === "*" &&
    dayOfWeek === "*"
  ) {
    const interval = Number(minuteInterval[1]);
    if (interval >= 1 && interval <= 59) {
      return { ...DEFAULT_CUSTOM_SCHEDULE, mode: "minutes", interval };
    }
  }

  const hourInterval = /^\*\/(\d+)$/.exec(hour);
  if (
    minute === "0" &&
    hourInterval &&
    dayOfMonth === "*" &&
    month === "*" &&
    dayOfWeek === "*"
  ) {
    const interval = Number(hourInterval[1]);
    if (interval >= 1 && interval <= 23) {
      return { ...DEFAULT_CUSTOM_SCHEDULE, mode: "hours", interval };
    }
  }

  if (
    /^\d+$/.test(minute) &&
    /^\d+$/.test(hour) &&
    /^\d+$/.test(dayOfMonth) &&
    dayOfWeek === "*"
  ) {
    const parsedMinute = Number(minute);
    const parsedHour = Number(hour);
    const parsedDay = Number(dayOfMonth);
    const parsedMonth = month === "*" ? null : Number(month);
    const validMonth =
      month === "*" ||
      (/^\d+$/.test(month) &&
        parsedMonth !== null &&
        parsedMonth >= 1 &&
        parsedMonth <= 12);
    if (
      parsedMinute <= 59 &&
      parsedHour <= 23 &&
      parsedDay >= 1 &&
      parsedDay <= 31 &&
      validMonth
    ) {
      return {
        ...DEFAULT_CUSTOM_SCHEDULE,
        mode: month === "*" ? "monthly" : "yearly",
        month: parsedMonth ?? DEFAULT_CUSTOM_SCHEDULE.month,
        dayOfMonth: parsedDay,
        hour: parsedHour,
        minute: parsedMinute,
      };
    }
  }

  return null;
}

export function customScheduleLabel(rawCron: string | undefined): string {
  const config = parseCustomSchedule(rawCron);
  if (!config) return rawCron ? "旧版自定义规则" : "尚未配置自定义规则";
  if (config.mode === "daily") {
    return `每日 ${pad2(config.hour)}:${pad2(config.minute)}`;
  }
  if (config.mode === "weekly") {
    const weekdayLabels: Record<string, string> = {
      mon: "一",
      tue: "二",
      wed: "三",
      thu: "四",
      fri: "五",
      sat: "六",
      sun: "日",
    };
    const days = config.daysOfWeek
      .map((day) => weekdayLabels[day] ?? day)
      .join("、");
    return `每周${days} ${pad2(config.hour)}:${pad2(config.minute)}`;
  }
  if (config.mode === "minutes") return `每隔 ${config.interval} 分钟`;
  if (config.mode === "hours") return `每隔 ${config.interval} 小时`;
  if (config.mode === "yearly") {
    return `每年 ${config.month} 月 ${config.dayOfMonth} 日 ${pad2(
      config.hour,
    )}:${pad2(config.minute)}`;
  }
  return `每月 ${config.dayOfMonth} 日 ${pad2(config.hour)}:${pad2(
    config.minute,
  )}`;
}
