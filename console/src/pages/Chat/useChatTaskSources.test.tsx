import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useChatTaskSources } from "./useChatTaskSources";
import { listTaskSources, tagTask } from "@/api/modules/cronTaskSources";
import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";
import type { TaskSourceSnapshot } from "@/api/types/cronTaskSource";

vi.mock("@/api/modules/cronTaskSources", async (original) => ({
  ...(await original<object>()),
  listTaskSources: vi.fn(),
}));
const context = {
  actor_id: "u",
  tenant_id: "u",
  source_id: "s",
  agent_id: "a",
};
afterEach(cleanup);
const snapshot = (name: string): TaskSourceSnapshot => ({
  legacy: [tagTask({ id: name, name } as never, "legacy", context)],
  scheduler: [],
  errors: [],
});
beforeEach(() => {
  vi.clearAllMocks();
  useIframeStore.setState({
    userId: "u",
    source: "s",
    manager: false,
    isSuperManager: false,
  });
  useAgentStore.setState({ selectedAgent: "a" });
});
it("Agent 切换立即隔离旧数据，迟到响应不能覆盖新列表", async () => {
  let resolve!: (value: TaskSourceSnapshot) => void;
  vi.mocked(listTaskSources)
    .mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    )
    .mockResolvedValueOnce(snapshot("new"));
  const { result } = renderHook(() => useChatTaskSources());
  await act(async () => {
    useAgentStore.setState({ selectedAgent: "b" });
  });
  await waitFor(() => expect(result.current.jobs[0]?.id).toBe("new"));
  await act(async () => {
    resolve(snapshot("old"));
  });
  expect(result.current.jobs.map((job) => job.id)).toEqual(["new"]);
});
it("来源查询失败保留已知任务，并提供明确错误供页面展示", async () => {
  vi.mocked(listTaskSources)
    .mockResolvedValueOnce(snapshot("known"))
    .mockResolvedValueOnce({
      legacy: null,
      scheduler: [],
      errors: ["旧任务列表暂不可用，保留已知任务"],
    });
  const { result } = renderHook(() => useChatTaskSources());
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  await act(async () => {
    await result.current.refreshJobs();
  });
  expect(result.current.jobs[0].id).toBe("known");
  expect(result.current.error).toContain("保留已知任务");
});
