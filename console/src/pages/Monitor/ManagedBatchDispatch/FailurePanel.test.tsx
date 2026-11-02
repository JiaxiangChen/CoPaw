import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import FailurePanel from "./FailurePanel";

const api = vi.hoisted(() => ({ failures: vi.fn(), retry: vi.fn() }));
vi.mock("../../../api/modules/managedBatchBoard", async () => ({
  ...(await vi.importActual("../../../api/modules/managedBatchBoard")),
  managedBatchBoard: api,
}));
const candidate = {
  member_id: "member",
  execution_id: "execution",
  member_version: 1,
  execution_version: 5,
  definition_version: 1,
  business_attempt: 1,
};
const command = {
  request_id: "saved-request",
  candidates: [candidate],
  confirm_stopped: true,
};
beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  api.failures.mockResolvedValue({
    items: [],
    counts: [],
    has_more: false,
    matched_count: 0,
  });
});
afterEach(cleanup);
it("长错误采用单行省略且悬停可查看完整内容，不撑高分页行", async () => {
  const message = "子任务执行失败：" + "完整错误详情。".repeat(40);
  api.failures.mockResolvedValue({
    counts: [{ failure_type: "other", label: "其他错误", count: 1 }],
    items: [
      {
        ...candidate,
        execution_id: "execution",
        tenant_id: "user",
        failure_type: "other",
        error_code: "cron_async_subtasks_failed",
        error_message: message,
      },
    ],
    has_more: false,
    matched_count: 1,
  });
  render(
    <FailurePanel
      source="bank"
      actor="manager"
      batchId="batch"
      onQueued={() => {}}
    />,
  );
  const error = await screen.findByText(message);
  expect(getComputedStyle(error.closest("td")!).whiteSpace).toBe("nowrap");
  const kind = screen.getByText("业务重试（新快照）");
  expect(getComputedStyle(kind.closest("td")!).whiteSpace).toBe("nowrap");
  fireEvent.mouseOver(error);
  expect(await screen.findByRole("tooltip")).toHaveTextContent(message);
});

it("响应丢失时保留并重提原始请求，不按新预览换请求ID", async () => {
  const key = `managed-batch-retry:${JSON.stringify([
    "bank",
    "manager",
    "batch",
  ])}`;
  sessionStorage.setItem(key, JSON.stringify(command));
  api.retry
    .mockRejectedValueOnce(new Error("response_lost"))
    .mockResolvedValueOnce({ queued: 1, skipped: 0, reused: true });
  const onQueued = vi.fn();
  render(
    <FailurePanel
      source="bank"
      actor="manager"
      batchId="batch"
      onQueued={onQueued}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "核对并重提原请求" }),
  );
  await screen.findByText(/response_lost/);
  expect(JSON.parse(sessionStorage.getItem(key)!)).toEqual(command);
  fireEvent.click(screen.getByRole("button", { name: "核对并重提原请求" }));
  await waitFor(() => expect(onQueued).toHaveBeenCalledOnce());
  expect(api.retry.mock.calls[0]).toEqual(["bank", "batch", command]);
  expect(api.retry.mock.calls[1]).toEqual(api.retry.mock.calls[0]);
  expect(sessionStorage.getItem(key)).toBeNull();
});

it("失败预览展示原始错误说明，而不只是公开错误码", async () => {
  api.failures.mockResolvedValue({
    counts: [{ failure_type: "configuration", label: "配置错误", count: 1 }],
    items: [
      {
        ...candidate,
        tenant_id: "user",
        failure_type: "configuration",
        error_code: "cron_execution_failed",
        error_message: "workflow secret value missing: auth_token",
      },
    ],
  });
  render(
    <FailurePanel
      source="bank"
      actor="manager"
      batchId="batch"
      onQueued={() => {}}
    />,
  );
  expect(
    await screen.findByText("workflow secret value missing: auth_token"),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("columnheader", { name: "原始失败原因" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("columnheader", { name: "重试次数" }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("columnheader", { name: "业务尝试" })).toBeNull();
});
