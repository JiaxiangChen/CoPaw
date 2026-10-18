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
