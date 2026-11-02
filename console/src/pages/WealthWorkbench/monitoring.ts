export interface SceneMonitoring {
  schedule: {
    type: "daily" | "weekly";
    hour: number;
    minute: number;
    daysOfWeek: string[];
  };
  cycle: "today" | "within7days";
  expectedCompletionRate: number;
}

export function defaultMonitoring(): SceneMonitoring {
  return {
    schedule: { type: "daily", hour: 11, minute: 0, daysOfWeek: ["mon"] },
    cycle: "today",
    expectedCompletionRate: 60,
  };
}
