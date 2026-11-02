import { expect, it } from "vitest";
import { defaultMonitoring } from "./monitoring";
it("督办策略默认每日11:00、当日、60%，各场景默认值独立", () => {
  const first = defaultMonitoring();
  expect(first).toEqual({
    cycle: "today",
    expectedCompletionRate: 60,
    schedule: { type: "daily", hour: 11, minute: 0, daysOfWeek: ["mon"] },
  });
  first.schedule.daysOfWeek.push("tue");
  first.expectedCompletionRate = 80;
  expect(defaultMonitoring().schedule.daysOfWeek).toEqual(["mon"]);
  expect(defaultMonitoring().expectedCompletionRate).toBe(60);
});
