/**
 * Parse cron expression to form-friendly format and vice versa.
 * Supports: hourly, daily, weekly, custom
 *
 * Day-of-week values use three-letter English abbreviations
 * (mon, tue, wed, thu, fri, sat, sun) to avoid the numbering
 * mismatch between crontab (0=Sun) and APScheduler v3 (0=Mon).
 */

export enum CronType {
  hourly = "hourly",
  daily = "daily",
  weekly = "weekly",
  monthly = "monthly",
  yearly = "yearly",
  interval = "interval",
  custom = "custom",
}

export interface CronParts {
  type: CronType;
  hour?: number;
  minute?: number;
  // 每天多点运行：例如 "0 9,10,11 * * *" 的 hour 拆分为 [9,10,11]
  hours?: number[];
  // 分钟步进间隔：例如 "/5 * * * *" 前缀带星号，step 为 5
  minuteStep?: number;
  // 是否每分钟运行（minute 字段为 "*"）
  everyMinute?: boolean;
  daysOfWeek?: string[]; // "mon", "tue", ..., "sun"
  // 具体日期：dayOfMonth + month，例如 "0 9 20 5 *" 表示 5 月 20 日
  day?: number;
  month?: number;
  rawCron?: string;
}

const CRON_RE = /^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$/;
const INTEGER_RE = /^\d+$/;

/**
 * Mapping from crontab numeric day to three-letter abbreviation.
 * Supports both crontab (0=Sun) and the common 7=Sun alias.
 */
const ORDERED_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
type DayName = (typeof ORDERED_DAYS)[number];

const NUM_TO_NAME: Record<string, DayName> = {
  "0": "sun",
  "1": "mon",
  "2": "tue",
  "3": "wed",
  "4": "thu",
  "5": "fri",
  "6": "sat",
  "7": "sun",
};

const VALID_NAMES = new Set<DayName>(ORDERED_DAYS);

function isDayName(value: string): value is DayName {
  return VALID_NAMES.has(value as DayName);
}

/**
 * Parse cron expression to CronParts
 * Examples:
 *   "0 * * * *" -> hourly
 *   "0 9 * * *" -> daily at 09:00
 *   "0 9 * * mon,wed,fri" -> weekly on Mon/Wed/Fri at 09:00
 *   "* /15 * * * *" -> custom (every 15 minutes)
 */
export function parseCron(cron: string): CronParts {
  const trimmed = (cron || "").trim();
  if (!trimmed) {
    return { type: CronType.daily, hour: 9, minute: 0 };
  }

  const match = trimmed.match(CRON_RE);
  if (!match) {
    return { type: CronType.custom, rawCron: trimmed };
  }

  const [, minute, hour, dayOfMonth, month, dayOfWeek] = match;

  // Interval / every-minute: "MA * * * *" where hour/dom/month/dow are all "*"
  //   minute = "*/N"  -> every N minutes
  //   minute = "*"    -> every minute (== */1)
  //   minute = "0"    -> hourly (once per hour at minute 0)
  if (
    hour === "*" &&
    dayOfMonth === "*" &&
    month === "*" &&
    dayOfWeek === "*"
  ) {
    if (minute === "0") {
      // Hourly: "0 * * * *"
      return { type: CronType.hourly, minute: 0 };
    }

    if (minute === "*" || minute === "*/1") {
      return { type: CronType.interval, minuteStep: 1, everyMinute: true };
    }

    if (minute.startsWith("*/")) {
      const step = parsePlainCronNumber(minute.slice(2), 1, 59);
      if (step !== null) {
        return { type: CronType.interval, minuteStep: step };
      }
      return { type: CronType.custom, rawCron: trimmed };
    }

    return { type: CronType.custom, rawCron: trimmed };
  }

  // Daily: "M H * * *" or multi-hour "M H1,H2,… * * *"
  if (dayOfMonth === "*" && month === "*" && dayOfWeek === "*") {
    const hours = parseHoursList(hour);
    const m = parsePlainCronNumber(minute, 0, 59);
    if (hours.length > 0 && m !== null) {
      return { type: CronType.daily, hours, minute: m };
    }
  }

  // Weekly: "M H * * D" where D is days
  if (dayOfMonth === "*" && month === "*" && dayOfWeek !== "*") {
    const h = parsePlainCronNumber(hour, 0, 23);
    const m = parsePlainCronNumber(minute, 0, 59);
    if (h !== null && m !== null) {
      const days = parseDaysOfWeek(dayOfWeek);
      if (days.length > 0) {
        return { type: CronType.weekly, hour: h, minute: m, daysOfWeek: days };
      }
    }
  }

  // Specific date schedule (dayOfWeek == "*").
  //  - "M H D * *"      -> style month *    : every month on the D-th day  => monthly
  //  - "M H D Mo *"     -> day + month given: once a year on Mo month D day => yearly
  if (dayOfWeek === "*") {
    const m = parsePlainCronNumber(minute, 0, 59);
    const h = parsePlainCronNumber(hour, 0, 23);
    const d = parsePlainCronNumber(dayOfMonth, 1, 31);
    const hookValid = m !== null && h !== null && d !== null;
    if (!hookValid) {
      return { type: CronType.custom, rawCron: trimmed };
    }

    if (month === "*") {
      // Every month on the given day of month.
      return { type: CronType.monthly, hour: h, minute: m, day: d };
    }

    const mo = parsePlainCronNumber(month, 1, 12);
    if (mo !== null) {
      // Fixed month + day of month: once per year.
      return { type: CronType.yearly, hour: h, minute: m, day: d, month: mo };
    }

    return { type: CronType.custom, rawCron: trimmed };
  }

  // Everything else is custom
  return { type: CronType.custom, rawCron: trimmed };
}

/**
 * Parse a (possibly multi-value) hour field into a sorted number list.
 *   "9"           -> [9]
 *   "9,10,11"     -> [9,10,11]
 *   "9-11"        -> [9,10,11]
 *   "9-11,13"     -> [9,10,11,13]
 * Returns [] for anything lossy / non-numeric (callers fall back to custom).
 */
function parseHoursList(hour: string): number[] {
  const seen = new Set<number>();
  const result: number[] = [];
  let invalid = false;

  const push = (value: number) => {
    if (value >= 0 && value <= 23 && !seen.has(value)) {
      seen.add(value);
      result.push(value);
    } else {
      // out-of-range hour is invalid
      invalid = true;
    }
  };

  const parts = hour.split(",");

  for (const part of parts) {
    const trimmed = part.trim();
    if (!trimmed) {
      return [];
    }

    if (trimmed.includes("-")) {
      const rangeParts = trimmed.split("-");
      if (rangeParts.length !== 2) {
        return [];
      }
      const startNum = Number(rangeParts[0]);
      const endNum = Number(rangeParts[1]);
      if (!Number.isInteger(startNum) || !Number.isInteger(endNum)) {
        return [];
      }
      if (startNum < 0 || startNum > 23 || endNum < 0 || endNum > 23) {
        invalid = true;
        continue;
      }
      if (startNum > endNum) {
        return [];
      }
      for (let i = startNum; i <= endNum; i++) {
        push(i);
      }
    } else {
      const num = Number(trimmed);
      if (!Number.isInteger(num) && trimmed !== "") {
        return [];
      }
      push(num);
    }
  }

  if (invalid) {
    return [];
  }

  result.sort((a, b) => a - b);
  return result;
}

function parsePlainCronNumber(
  value: string,
  min: number,
  max: number,
): number | null {
  if (!INTEGER_RE.test(value)) {
    return null;
  }

  const parsed = Number(value);
  if (parsed < min || parsed > max) {
    return null;
  }

  return parsed;
}

/**
 * Serialize CronParts back to cron expression
 */
export function serializeCron(parts: CronParts): string {
  switch (parts.type) {
    case CronType.hourly:
      return "0 * * * *";

    case CronType.interval: {
      const step = Math.max(1, parts.minuteStep ?? 1);
      return parts.everyMinute || step === 1 ? "* * * * *" : `*/${step} * * * *`;
    }

    case CronType.daily: {
      const m = parts.minute ?? 0;
      if (parts.hours && parts.hours.length > 0) {
        return `${m} ${parts.hours.join(",")} * * *`;
      }
      const h = parts.hour ?? 9;
      return `${m} ${h} * * *`;
    }

    case CronType.weekly: {
      const h = parts.hour ?? 9;
      const m = parts.minute ?? 0;
      const days = serializeDaysOfWeek(parts.daysOfWeek);
      return `${m} ${h} * * ${days}`;
    }

    case CronType.monthly: {
      const h = parts.hour ?? 9;
      const m = parts.minute ?? 0;
      const d = parts.day ?? 1;
      // month is "*" -> every month on the d-th day
      if (parts.month === undefined) {
        return `${m} ${h} ${d} * *`;
      }
      const mo = parts.month ?? 1;
      return `${m} ${h} ${d} ${mo} *`;
    }

    case CronType.yearly: {
      const h = parts.hour ?? 9;
      const m = parts.minute ?? 0;
      const d = parts.day ?? 1;
      const mo = parts.month ?? 1;
      return `${m} ${h} ${d} ${mo} *`;
    }

    case CronType.custom:
      return parts.rawCron || "0 9 * * *";

    default:
      return "0 9 * * *";
  }
}

/**
 * Parse day of week field to string abbreviations.
 *
 * Accepts both numeric (crontab convention: 0=Sun … 6=Sat) and
 * named values (mon, tue, …). Always returns abbreviation strings.
 * Invalid or lossy tokens return an empty array so callers can
 * fall back to `custom`.
 */
function parseDaysOfWeek(dayOfWeek: string): string[] {
  const days: DayName[] = [];
  const parts = dayOfWeek.split(",");

  for (const part of parts) {
    const trimmed = part.trim().toLowerCase();

    if (!trimmed) {
      return [];
    }

    if (isDayName(trimmed)) {
      if (!days.includes(trimmed)) {
        days.push(trimmed);
      }
      continue;
    }

    if (trimmed.includes("-")) {
      const rangeParts = trimmed.split("-");
      if (
        rangeParts.length !== 2 ||
        rangeParts[0] === "" ||
        rangeParts[1] === ""
      ) {
        return [];
      }

      const [startStr, endStr] = rangeParts;
      const startName = NUM_TO_NAME[startStr] || startStr;
      const endName = NUM_TO_NAME[endStr] || endStr;

      if (!isDayName(startName) || !isDayName(endName)) {
        return [];
      }

      const si = ORDERED_DAYS.indexOf(startName);
      const ei = ORDERED_DAYS.indexOf(endName);
      if (si === -1 || ei === -1 || si > ei) {
        return [];
      }

      for (let i = si; i <= ei; i++) {
        if (!days.includes(ORDERED_DAYS[i])) {
          days.push(ORDERED_DAYS[i]);
        }
      }
      continue;
    }

    const name = NUM_TO_NAME[trimmed];
    if (!name) {
      return [];
    }

    if (!days.includes(name)) {
      days.push(name);
    }
  }

  return days;
}

function serializeDaysOfWeek(daysOfWeek?: string[]): string {
  if (!daysOfWeek || daysOfWeek.length === 0) {
    return "mon";
  }

  const selectedDays = ORDERED_DAYS.filter((day) => daysOfWeek.includes(day));
  if (selectedDays.length === 0) {
    return "mon";
  }

  const segments: string[] = [];
  let rangeStart = selectedDays[0];
  let previousDay = selectedDays[0];

  for (let i = 1; i <= selectedDays.length; i++) {
    const currentDay = selectedDays[i];
    const isContiguous =
      currentDay !== undefined &&
      ORDERED_DAYS.indexOf(currentDay) ===
      ORDERED_DAYS.indexOf(previousDay) + 1;

    if (isContiguous) {
      previousDay = currentDay;
      continue;
    }

    if (rangeStart === previousDay) {
      segments.push(rangeStart);
    } else {
      segments.push(`${rangeStart}-${previousDay}`);
    }

    rangeStart = currentDay;
    previousDay = currentDay ?? previousDay;
  }

  return segments.join(",");
}