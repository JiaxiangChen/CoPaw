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
import { useWealthStore } from "../store";
import { TargetPicker } from "./TargetPicker";

vi.mock("../../../api/request", () => ({ request: vi.fn() }));
const mockRequest = vi.mocked(request);
const branches = [
  { mOrgCode: "102", mOrgName: "深圳深湾中心", mOrgLvl: 50, leaf: false },
  { mOrgCode: "103", mOrgName: "另一支行", mOrgLvl: 70, leaf: true },
];
const people = (sapID: string, userName: string) => ({
  code: "success",
  errMsg: null,
  data: [{ sapID, userName, branchName: "某分行", subBranchName: null }],
});

beforeEach(() => {
  mockRequest.mockReset();
  useIframeStore.getState().setContext({
    bbk: "755",
    orgCode: "755480",
    orgLvl: "70",
    sysId: "cookie-system",
  });
  useWealthStore.setState({
    accountId: "middle",
    targets: [],
    targetSapIds: [],
    targetsLoaded: false,
    targetBranch: null,
    branchTargetSapIds: [],
  });
});
afterEach(cleanup);

describe("中台按支行累计分发", () => {
  it.each([null, "", " ", "NaN", "70.5", "-1"])(
    "cookie 机构级别 %s 无效时不查询支行",
    async (orgLvl) => {
      useIframeStore.getState().setContext({ orgLvl });
      render(<TargetPicker />);
      await waitFor(() =>
        expect(
          screen.getByRole("option", { name: "当前分行暂无可选支行" }),
        ).toBeInTheDocument(),
      );
      expect(mockRequest).not.toHaveBeenCalled();
    },
  );
  it("缺少 cookie 系统标识时不查询支行", async () => {
    useIframeStore.getState().setContext({ sysId: null });
    render(<TargetPicker />);
    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: "当前分行暂无可选支行" }),
      ).toBeInTheDocument(),
    );
    expect(mockRequest).not.toHaveBeenCalled();
  });
  it("先查支行，按返回层级查人员，跨支行保留选择和姓名", async () => {
    mockRequest
      .mockResolvedValueOnce(branches)
      .mockResolvedValueOnce(people("0001", "张三"))
      .mockResolvedValueOnce(people("0002", "李四"));
    render(<TargetPicker />);
    expect(
      screen.getByText("请先选择支行，再选择分发人员"),
    ).toBeInTheDocument();
    const select = screen.getByRole("combobox", { name: "选择支行" });
    await waitFor(() => expect(select).toBeEnabled());
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(mockRequest).toHaveBeenNthCalledWith(
      1,
      "/user-info/branches/by-branch",
      {
        method: "POST",
        body: JSON.stringify({
          mOrgCode: "755",
          mOrgLvl: 70,
          sysId: "cookie-system",
        }),
      },
    );
    fireEvent.change(select, { target: { value: "102" } });
    fireEvent.click(await screen.findByRole("button", { name: /张三/ }));
    expect(mockRequest).toHaveBeenNthCalledWith(
      2,
      "/user-info/positions/by-branch",
      {
        method: "POST",
        body: JSON.stringify({ bbkOrgId: "755", mOrgCode: "102", mOrgLvl: 50 }),
      },
    );
    fireEvent.change(select, { target: { value: "103" } });
    fireEvent.click(await screen.findByRole("button", { name: /李四/ }));
    expect(useWealthStore.getState().targetSapIds).toEqual(["0001", "0002"]);
    expect(
      screen.getByRole("button", { name: "移除张三" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "移除李四" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(useWealthStore.getState().targetSapIds).toEqual(["0001"]);
    fireEvent.click(screen.getByRole("button", { name: "移除张三" }));
    expect(useWealthStore.getState().targetSapIds).toEqual([]);
  });

  it("快速切换时旧响应不能覆盖当前支行", async () => {
    let finishOld!: (value: unknown) => void;
    mockRequest
      .mockReturnValueOnce(
        new Promise((resolve) => {
          finishOld = resolve;
        }),
      )
      .mockResolvedValueOnce(people("0002", "李四"));
    const oldLoad = useWealthStore.getState().loadTargets(branches[0]);
    await useWealthStore.getState().loadTargets(branches[1]);
    finishOld(people("0001", "张三"));
    await oldLoad;
    expect(useWealthStore.getState().targetBranch?.mOrgCode).toBe("103");
    expect(useWealthStore.getState().branchTargetSapIds).toEqual(["0002"]);
    expect(useWealthStore.getState().targets.map((t) => t.sapId)).toEqual([
      "0002",
    ]);
  });

  it("行长直接用登录支行查询，不显示支行选择", async () => {
    useWealthStore.setState({ accountId: "president" });
    mockRequest.mockResolvedValueOnce(people("0001", "张三"));
    render(<TargetPicker />);
    expect(
      screen.queryByRole("combobox", { name: "选择支行" }),
    ).not.toBeInTheDocument();
    await screen.findByRole("button", { name: /张三/ });
    expect(mockRequest).toHaveBeenCalledWith("/user-info/positions/by-branch", {
      method: "POST",
      body: JSON.stringify({
        bbkOrgId: "755",
        mOrgCode: "755480",
        mOrgLvl: 70,
      }),
    });
  });
});
