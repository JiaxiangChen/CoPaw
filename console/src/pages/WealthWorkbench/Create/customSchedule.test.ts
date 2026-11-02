import { describe, expect, it } from "vitest";
import {
  DEFAULT_CUSTOM_SCHEDULE,
  buildCustomCron,
  customScheduleLabel,
  parseCustomSchedule,
} from "./customSchedule";

describe("customSchedule", () => {
  it("自定义名单推送默认08:00", () => {
    expect(DEFAULT_CUSTOM_SCHEDULE.hour).toBe(8);
    expect(DEFAULT_CUSTOM_SCHEDULE.minute).toBe(0);
    expect(buildCustomCron({ ...DEFAULT_CUSTOM_SCHEDULE, mode: "daily" })).toBe(
      "0 8 * * *",
    );
  });

  it("将按分钟间隔转换为 cron", () => {
    expect(
      buildCustomCron({
        mode: "minutes",
        interval: 15,
        month: 1,
        dayOfMonth: 1,
        hour: 9,
        minute: 0,
        daysOfWeek: ["mon"],
      }),
    ).toBe("*/15 * * * *");
  });

  it("将按小时间隔转换为 cron", () => {
    expect(
      buildCustomCron({
        mode: "hours",
        interval: 4,
        month: 1,
        dayOfMonth: 1,
        hour: 9,
        minute: 0,
        daysOfWeek: ["mon"],
      }),
    ).toBe("0 */4 * * *");
  });

  it("将每月指定日期与时刻转换为 cron", () => {
    expect(
      buildCustomCron({
        mode: "monthly",
        interval: 1,
        month: 1,
        dayOfMonth: 10,
        hour: 14,
        minute: 30,
        daysOfWeek: ["mon"],
      }),
    ).toBe("30 14 10 * *");
  });

  it("将每年指定日期与时刻转换为 cron", () => {
    expect(
      buildCustomCron({
        mode: "yearly",
        interval: 1,
        month: 6,
        dayOfMonth: 10,
        hour: 14,
        minute: 30,
        daysOfWeek: ["mon"],
      }),
    ).toBe("30 14 10 6 *");
  });

  it("将自定义区的每日和每周规则转换为 cron", () => {
    expect(
      buildCustomCron({
        ...DEFAULT_CUSTOM_SCHEDULE,
        mode: "daily",
        hour: 9,
        minute: 15,
      }),
    ).toBe("15 9 * * *");
    expect(
      buildCustomCron({
        ...DEFAULT_CUSTOM_SCHEDULE,
        mode: "weekly",
        hour: 9,
        minute: 15,
        daysOfWeek: ["mon", "fri"],
      }),
    ).toBe("15 9 * * mon,fri");
  });

  it("能解析可视化编辑器生成的规则并展示可读摘要", () => {
    expect(parseCustomSchedule("*/15 * * * *")).toMatchObject({
      mode: "minutes",
      interval: 15,
    });
    expect(parseCustomSchedule("0 */4 * * *")).toMatchObject({
      mode: "hours",
      interval: 4,
    });
    expect(parseCustomSchedule("30 14 10 * *")).toMatchObject({
      mode: "monthly",
      dayOfMonth: 10,
      hour: 14,
      minute: 30,
    });
    expect(customScheduleLabel("30 14 10 * *")).toBe("每月 10 日 14:30");
    expect(parseCustomSchedule("30 14 10 6 *")).toMatchObject({
      mode: "yearly",
      month: 6,
      dayOfMonth: 10,
      hour: 14,
      minute: 30,
    });
    expect(customScheduleLabel("30 14 10 6 *")).toBe("每年 6 月 10 日 14:30");
  });

  it("不猜测旧版复杂 cron 的含义", () => {
    expect(parseCustomSchedule("0 9 1,15 * *")).toBeNull();
    expect(customScheduleLabel("0 9 1,15 * *")).toBe("旧版自定义规则");
  });
});
