import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useCronReportRead } from "./useCronReportRead";
import { resolveTaskOrigin } from "@/api/modules/cronTaskSources";
import { markTaskReferenceRead } from "@/api/modules/cronTaskActions";
import { cronAccessKey, currentCronContext } from "@/api/cronTaskContext";
import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";
vi.mock("@/api/modules/cronTaskSources", () => ({
  resolveTaskOrigin: vi.fn(),
}));
vi.mock("@/api/modules/cronTaskActions", () => ({
  markTaskReferenceRead: vi.fn(),
}));
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  useIframeStore.setState({
    userId: "u",
    source: "s",
    manager: false,
    isSuperManager: false,
  });
  useAgentStore.setState({ selectedAgent: "a" });
  vi.mocked(markTaskReferenceRead).mockResolvedValue({});
});
const origin = () => ({
  owner: "scheduler" as const,
  context: currentCronContext(),
  accessKey: cronAccessKey(),
});
it("当前 Chat 已有可信来源时直接走对应已读接口，不再探测归属", async () => {
  const known = origin();
  renderHook(() =>
    useCronReportRead({
      open: true,
      taskId: "j",
      contentKey: "report-1",
      knownTaskId: "j",
      knownOrigin: known,
      onFailure: vi.fn(),
    }),
  );
  await waitFor(() =>
    expect(markTaskReferenceRead).toHaveBeenCalledWith(
      "j",
      known,
      true,
      expect.any(String),
    ),
  );
  expect(resolveTaskOrigin).not.toHaveBeenCalled();
});
it("裸 ID 只读核对失败时不写任何已读接口，并提示未清理", async () => {
  const failed = vi.fn();
  vi.mocked(resolveTaskOrigin).mockRejectedValue(new Error("无法确认"));
  renderHook(() =>
    useCronReportRead({
      open: true,
      taskId: "j",
      contentKey: "report-1",
      onFailure: failed,
    }),
  );
  await waitFor(() => expect(failed).toHaveBeenCalled());
  expect(markTaskReferenceRead).not.toHaveBeenCalled();
});
it("确认归属期间关闭预览或切换 Agent，迟到结果不继续写入", async () => {
  let resolve!: (value: ReturnType<typeof origin>) => void;
  vi.mocked(resolveTaskOrigin).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const captured = origin();
  const failed = vi.fn();
  const { rerender } = renderHook(
    ({ open }) =>
      useCronReportRead({
        open,
        taskId: "j",
        contentKey: "report-1",
        onFailure: failed,
      }),
    { initialProps: { open: true } },
  );
  rerender({ open: false });
  await act(async () => {
    useAgentStore.setState({ selectedAgent: "b" });
    resolve(captured);
  });
  expect(markTaskReferenceRead).not.toHaveBeenCalled();
  expect(failed).not.toHaveBeenCalled();
});
