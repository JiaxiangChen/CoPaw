import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import BatchPriorityEditor from "./BatchPriorityEditor";
import { batchOperations } from "../../../../api/modules/batchOperations";

vi.mock("../../../../api/modules/batchOperations", () => ({
  batchOperations: { savePriority: vi.fn() },
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("新版可以注入保存协议，绝不调用旧接口", async () => {
  const savePriority = vi.fn().mockResolvedValue({});
  render(
    <BatchPriorityEditor
      jobId="new-parent"
      initial={{ user_ids: ["alice", "bob"] }}
      savePriority={savePriority}
    />,
  );
  fireEvent.click(screen.getByLabelText("优先用户 bob 上移"));
  fireEvent.click(screen.getByText("保存优先策略"));
  await screen.findByText("优先策略已保存，从新批次生效");
  expect(savePriority).toHaveBeenCalledWith({
    user_ids: ["bob", "alice"],
    branch_ids: [],
  });
  expect(batchOperations.savePriority).not.toHaveBeenCalled();
});

it("saves the reordered user and primary-branch lists", async () => {
  vi.mocked(batchOperations.savePriority).mockResolvedValue({});
  render(
    <BatchPriorityEditor
      jobId="parent"
      initial={{ user_ids: ["alice", "bob"], branch_ids: ["121", "110"] }}
    />,
  );
  expect(
    within(screen.getByRole("list", { name: "优先用户排序" })).getAllByRole(
      "listitem",
    ),
  ).toHaveLength(2);
  expect(
    within(screen.getByRole("list", { name: "优先一级分行排序" })).getAllByRole(
      "listitem",
    ),
  ).toHaveLength(2);
  fireEvent.click(screen.getByLabelText("优先用户 bob 上移"));
  fireEvent.click(screen.getByLabelText("优先一级分行 110 上移"));
  fireEvent.click(screen.getByText("保存优先策略"));
  await screen.findByText("优先策略已保存，从新批次生效");
  expect(batchOperations.savePriority).toHaveBeenCalledWith("parent", {
    user_ids: ["bob", "alice"],
    branch_ids: ["110", "121"],
  });
});

it("explains empty priorities and keeps saving as the primary action", () => {
  render(<BatchPriorityEditor jobId="parent" initial={{}} />);
  expect(screen.getByText("暂未设置优先用户")).toBeInTheDocument();
  expect(screen.getByText("暂未设置优先一级分行")).toBeInTheDocument();
  expect(screen.getByText("保存后对新批次生效")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存优先策略" })).toHaveClass(
    "ant-btn-primary",
  );
});

it("keeps edited values available after a failed save", async () => {
  vi.mocked(batchOperations.savePriority).mockRejectedValue(
    new Error("同步失败"),
  );
  render(
    <BatchPriorityEditor
      jobId="parent"
      initial={{ user_ids: ["alice"], branch_ids: [] }}
    />,
  );
  fireEvent.click(screen.getByText("保存优先策略"));
  await screen.findByText("同步失败");
  await waitFor(() =>
    expect(
      screen.getByText("保存优先策略").closest("button"),
    ).not.toBeDisabled(),
  );
  expect(screen.getByLabelText("优先用户 alice 移除")).toBeInTheDocument();
});
