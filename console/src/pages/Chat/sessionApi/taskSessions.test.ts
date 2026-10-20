import { expect, it } from "vitest";
import { filterStaleTaskSessions } from "./taskSessions";
const sessions = [
  {
    id: "old",
    meta: {
      session_kind: "task",
      task_job_id: "same",
      management_owner: "legacy",
    },
  },
  {
    id: "new",
    meta: {
      session_kind: "task",
      task_job_id: "same",
      management_owner: "scheduler",
    },
  },
  { id: "unknown", meta: { session_kind: "task", task_job_id: "missing" } },
  { id: "normal", meta: {} },
];
it("相同任务 ID 按真实会话来源分别判断是否仍存在", () => {
  expect(
    filterStaleTaskSessions(sessions, {
      legacy: new Set(["same"]),
      scheduler: new Set(),
    }).map((session) => session.id),
  ).toEqual(["old", "normal"]);
});
it("失败来源和来源不明的会话保留，不作为已删除清掉", () => {
  expect(
    filterStaleTaskSessions(sessions, {
      legacy: new Set(),
      scheduler: null,
    }).map((session) => session.id),
  ).toEqual(["new", "unknown", "normal"]);
});
it("旧 Set 契约仍保留原过滤语义", () => {
  expect(filterStaleTaskSessions(sessions, new Set(["same"]))).toHaveLength(3);
});
