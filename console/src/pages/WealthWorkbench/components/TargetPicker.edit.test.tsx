import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../../../api/request";
import { useIframeStore } from "../../../stores/iframeStore";
import { fetchBootstrap } from "../api";
import { useWealthStore } from "../store";
import { TargetPicker } from "./TargetPicker";

vi.mock("../../../api/request", () => ({ request: vi.fn() }));
const mockRequest = vi.mocked(request);
const planView = {
  id: "edit-plan",
  name: "待修改规划",
  publish_status: "published",
  board_status: "已自动下发",
  editable: true,
  scenes: [],
  targets: [
    { sap_id: "0001", name: "张三" },
    { sap_id: "0002", name: "李四" },
  ],
};

beforeEach(() => {
  mockRequest.mockReset();
  useIframeStore
    .getState()
    .setContext({
      bbk: "755",
      orgCode: "755480",
      orgLvl: "70",
      sysId: "retail",
    });
  useWealthStore.setState({
    accountId: "middle",
    plans: [],
    targets: [],
    targetSapIds: [],
    targetsLoaded: false,
    targetBranch: null,
    branchTargetSapIds: [],
    editingId: null,
  });
});
afterEach(cleanup);

async function openEditPlan() {
  mockRequest.mockResolvedValueOnce({ items: [planView] });
  const { plans } = await fetchBootstrap("middle");
  useWealthStore.setState({ plans });
  useWealthStore.getState().editPlan("edit-plan");
}

describe("修改规划分发人员回显", () => {
  it("无人员缓存时从规划接口恢复姓名，支行列表初始化不清空已选人员", async () => {
    await openEditPlan();
    mockRequest.mockResolvedValueOnce([]);
    render(<TargetPicker />);
    expect(
      screen.getByRole("button", { name: "移除张三" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "移除李四" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("当前分行暂无可选支行")).toBeInTheDocument(),
    );
    expect(useWealthStore.getState().targetSapIds).toEqual(["0001", "0002"]);
    fireEvent.click(screen.getByRole("button", { name: "移除张三" }));
    expect(useWealthStore.getState().targetSapIds).toEqual(["0002"]);
  });

  it("行长加载当前人员后保留其他已保存人员及姓名", async () => {
    await openEditPlan();
    useWealthStore.setState({ accountId: "president" });
    mockRequest.mockResolvedValueOnce({
      code: "success",
      data: [
        {
          sapID: "0001",
          userName: "张三新姓名",
          branchName: "某分行",
          subBranchName: null,
        },
      ],
    });
    render(<TargetPicker />);
    await screen.findByRole("button", { name: "移除张三新姓名" });
    expect(
      screen.getByRole("button", { name: "移除李四" }),
    ).toBeInTheDocument();
    expect(useWealthStore.getState().targetSapIds).toEqual(["0001", "0002"]);
  });

  it("无姓名的历史目标用 ID 回显，并清除上一份计划的人员缓存", async () => {
    await openEditPlan();
    useWealthStore.setState({
      plans: [
        {
          ...useWealthStore.getState().plans[0],
          id: "old-plan",
          targetSapIds: ["0003"],
          targets: undefined,
        },
      ],
    });
    useWealthStore.getState().editPlan("old-plan");
    mockRequest.mockResolvedValueOnce([]);
    render(<TargetPicker />);
    expect(
      screen.getByRole("button", { name: "移除0003" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "移除张三" }),
    ).not.toBeInTheDocument();
  });
});
