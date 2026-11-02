import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
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
const branchResponse = (data: unknown[]) => ({
  code: "200",
  message: "ok",
  success: true,
  data,
});
const people = (sapID: string, userName: string) => ({
  code: "200",
  message: "ok",
  success: true,
  data: [{ sapID, userName, branchName: "某分行", subBranchName: null }],
});

beforeEach(() => {
  mockRequest.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string, options?: RequestInit) =>
        new Response(JSON.stringify(await mockRequest(path, options))),
    ),
  );
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
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("中台按支行累计分发", () => {
  it.each([false, true])("121516 在子层=%s 时直接查人员", async (nested) => {
    const special = {
      mOrgCode: "121516",
      mOrgName: "特殊机构",
      mOrgLvl: 70,
      leaf: false,
    };
    if (nested)
      mockRequest
        .mockResolvedValueOnce(branchResponse(branches))
        .mockResolvedValueOnce(people("parent", "上级人员"));
    mockRequest
      .mockResolvedValueOnce(branchResponse([special]))
      .mockResolvedValueOnce(people("0001", "张三"));
    render(<TargetPicker />);
    const select = screen.getByRole("combobox", { name: "选择支行" });
    await waitFor(() => expect(select).toBeEnabled());
    fireEvent.mouseDown(select);
    if (nested)
      fireEvent.click(
        await screen.findByRole("menuitemcheckbox", { name: /深圳深湾中心/ }),
      );
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /特殊机构/ }),
    );
    await screen.findByRole("button", { name: /张三/ });
    expect(mockRequest).toHaveBeenCalledTimes(nested ? 4 : 2);
    expect(mockRequest).toHaveBeenLastCalledWith(
      "/zt/wealth/wmadm/getUserPostionByBrn",
      {
        method: "POST",
        signal: expect.any(AbortSignal),
        body: JSON.stringify({
          bbkOrgId: "755",
          mmOrgCode: "121516",
          mmOrgLvl: 70,
        }),
      },
    );
    const branchQueries = mockRequest.mock.calls.filter(
      ([url]) => url === "/zt/wealth/wmadm/branchTreeInfo",
    );
    expect(
      branchQueries.every(
        ([, options]) =>
          JSON.parse(String(options?.body)).mmOrgCode !== "121516",
      ),
    ).toBe(true);
  });
  it("探索新的上级时使旧人员查询失效", async () => {
    let finishOld!: (value: unknown) => void;
    mockRequest.mockReturnValueOnce(
      new Promise((resolve) => {
        finishOld = resolve;
      }),
    );
    const oldLoad = useWealthStore.getState().loadTargets(branches[0]);
    useWealthStore.getState().resetTargetBranch();
    finishOld(people("0001", "张三"));
    await oldLoad;
    expect(useWealthStore.getState().targetBranch).toBeNull();
    expect(useWealthStore.getState().branchTargetSapIds).toEqual([]);
    expect(useWealthStore.getState().targetsLoaded).toBe(false);
  });
  it("每选一级都查询人员，有下级也可停留在当前级", async () => {
    const child = {
      mOrgCode: "1021",
      mOrgName: "一级子支行",
      mOrgLvl: 60,
      leaf: true,
    };
    const last = {
      mOrgCode: "10211",
      mOrgName: "末级子支行",
      mOrgLvl: 70,
      leaf: false,
    };
    mockRequest
      .mockResolvedValueOnce(branchResponse(branches))
      .mockResolvedValueOnce(people("parent", "父级经理"))
      .mockResolvedValueOnce(branchResponse([child]))
      .mockResolvedValueOnce(people("child", "子级经理"))
      .mockResolvedValueOnce(branchResponse([last]))
      .mockResolvedValueOnce(people("0001", "张三"))
      .mockResolvedValueOnce(branchResponse([]));
    render(<TargetPicker />);
    const select = screen.getByRole("combobox", { name: "选择支行" });
    await waitFor(() => expect(select).toBeEnabled());
    fireEvent.mouseDown(select);
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /深圳深湾中心/ }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /父级经理/ }));
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /一级子支行/ }),
    );
    await screen.findByRole("button", { name: /子级经理/ });
    expect(
      within(screen.getByRole("group", { name: "分发目标列表" })).queryByText(
        "父级经理",
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "移除父级经理" }),
    ).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /末级子支行/ }),
    );
    await screen.findByRole("button", { name: /张三/ });
    expect(mockRequest).toHaveBeenNthCalledWith(
      3,
      "/zt/wealth/wmadm/branchTreeInfo",
      {
        method: "POST",
        signal: expect.any(AbortSignal),
        body: JSON.stringify({
          mmOrgCode: "102",
          mmOrgLvl: 50,
          sysId: "cookie-system",
        }),
      },
    );
    expect(mockRequest).toHaveBeenNthCalledWith(
      5,
      "/zt/wealth/wmadm/branchTreeInfo",
      {
        method: "POST",
        signal: expect.any(AbortSignal),
        body: JSON.stringify({
          mmOrgCode: "1021",
          mmOrgLvl: 60,
          sysId: "cookie-system",
        }),
      },
    );
    expect(mockRequest).toHaveBeenNthCalledWith(
      6,
      "/zt/wealth/wmadm/getUserPostionByBrn",
      {
        method: "POST",
        signal: expect.any(AbortSignal),
        body: JSON.stringify({
          bbkOrgId: "755",
          mmOrgCode: "10211",
          mmOrgLvl: 70,
        }),
      },
    );
  });

  it("下级查询未完成也可选当前级人员，回到缓存上级重新查人员", async () => {
    let finishChildren!: (value: unknown) => void;
    mockRequest
      .mockResolvedValueOnce(branchResponse(branches))
      .mockResolvedValueOnce(people("0001", "张三"))
      .mockReturnValueOnce(
        new Promise((resolve) => {
          finishChildren = resolve;
        }),
      )
      .mockResolvedValueOnce(people("0002", "李四"))
      .mockResolvedValueOnce(branchResponse([]))
      .mockResolvedValueOnce(people("0001", "张三"));
    render(<TargetPicker />);
    const select = screen.getByRole("combobox", { name: "选择支行" });
    await waitFor(() => expect(select).toBeEnabled());
    fireEvent.mouseDown(select);
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /深圳深湾中心/ }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /张三/ }));
    finishChildren(
      branchResponse([
        { mOrgCode: "1021", mOrgName: "子支行", mOrgLvl: 60, leaf: false },
      ]),
    );
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /子支行/ }),
    );
    await screen.findByRole("button", { name: /李四/ });
    fireEvent.mouseDown(select);
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /深圳深湾中心/ }),
    );
    await waitFor(() =>
      expect(useWealthStore.getState().branchTargetSapIds).toEqual(["0001"]),
    );
    expect(useWealthStore.getState().targetSapIds).toEqual(["0001"]);
    expect(mockRequest).toHaveBeenCalledTimes(6);
    expect(mockRequest).toHaveBeenLastCalledWith(
      "/zt/wealth/wmadm/getUserPostionByBrn",
      {
        method: "POST",
        signal: expect.any(AbortSignal),
        body: JSON.stringify({
          bbkOrgId: "755",
          mmOrgCode: "102",
          mmOrgLvl: 50,
        }),
      },
    );
  });

  it("子支行请求失败仍显示当前级人员，可重试下级查询", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    mockRequest
      .mockResolvedValueOnce(branchResponse(branches))
      .mockResolvedValueOnce(people("0001", "张三"))
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(people("0001", "张三"))
      .mockResolvedValueOnce(branchResponse([]));
    render(<TargetPicker />);
    const select = screen.getByRole("combobox", { name: "选择支行" });
    await waitFor(() => expect(select).toBeEnabled());
    fireEvent.mouseDown(select);
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /深圳深湾中心/ }),
    );
    await screen.findByRole("alert");
    await screen.findByRole("button", { name: /张三/ });
    expect(mockRequest).toHaveBeenCalledTimes(3);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByRole("button", { name: /张三/ });
    warn.mockRestore();
  });

  it("切换父支行后忽略旧的子支行响应", async () => {
    let finishOld!: (value: unknown) => void;
    mockRequest
      .mockResolvedValueOnce(branchResponse(branches))
      .mockResolvedValueOnce(people("0001", "张三"))
      .mockReturnValueOnce(
        new Promise((resolve) => {
          finishOld = resolve;
        }),
      )
      .mockResolvedValueOnce(people("0002", "李四"))
      .mockResolvedValueOnce(branchResponse([]));
    render(<TargetPicker />);
    const select = screen.getByRole("combobox", { name: "选择支行" });
    await waitFor(() => expect(select).toBeEnabled());
    fireEvent.mouseDown(select);
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /深圳深湾中心/ }),
    );
    await waitFor(() => expect(mockRequest).toHaveBeenCalledTimes(3));
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /另一支行/ }),
    );
    await screen.findByRole("button", { name: /李四/ });
    finishOld(
      branchResponse([
        { mOrgCode: "old", mOrgName: "过期子支行", mOrgLvl: 70, leaf: true },
      ]),
    );
    await waitFor(() =>
      expect(useWealthStore.getState().targetBranch?.mOrgCode).toBe("103"),
    );
    expect(screen.queryByText("过期子支行（old）")).not.toBeInTheDocument();
  });

  it("按支行名称或编码模糊搜索，不额外调用接口", async () => {
    mockRequest.mockResolvedValueOnce(branchResponse(branches));
    render(<TargetPicker />);
    const select = screen.getByRole("combobox", { name: "选择支行" });
    await waitFor(() => expect(select).toBeEnabled());
    fireEvent.mouseDown(select);
    fireEvent.change(select, { target: { value: "深湾" } });
    expect(
      await screen.findByRole("menuitemcheckbox", { name: /深圳深湾中心/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("menuitemcheckbox", { name: /另一支行/ }),
    ).not.toBeInTheDocument();
    fireEvent.change(select, { target: { value: "103" } });
    expect(
      await screen.findByRole("menuitemcheckbox", { name: /另一支行/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("menuitemcheckbox", { name: /深圳深湾中心/ }),
    ).not.toBeInTheDocument();
    fireEvent.change(select, { target: { value: "不存在" } });
    expect(await screen.findByText("未找到匹配的支行")).toBeInTheDocument();
    expect(mockRequest).toHaveBeenCalledTimes(1);
  });
  it.each([null, "", " ", "NaN", "70.5", "-1"])(
    "cookie 机构级别 %s 无效时不查询支行",
    async (orgLvl) => {
      useIframeStore.getState().setContext({ orgLvl });
      render(<TargetPicker />);
      await waitFor(() =>
        expect(screen.getByText("当前分行暂无可选支行")).toBeInTheDocument(),
      );
      expect(mockRequest).not.toHaveBeenCalled();
    },
  );
  it("缺少 cookie 系统标识时不查询支行", async () => {
    useIframeStore.getState().setContext({ sysId: null });
    render(<TargetPicker />);
    await waitFor(() =>
      expect(screen.getByText("当前分行暂无可选支行")).toBeInTheDocument(),
    );
    expect(mockRequest).not.toHaveBeenCalled();
  });
  it("先查支行，按返回层级查人员，跨支行保留选择和姓名", async () => {
    mockRequest
      .mockResolvedValueOnce(branchResponse(branches))
      .mockResolvedValueOnce(people("0001", "张三"))
      .mockResolvedValueOnce(branchResponse([]))
      .mockResolvedValueOnce(people("0002", "李四"))
      .mockResolvedValueOnce(branchResponse([]));
    render(<TargetPicker />);
    expect(
      screen.getByText("请先选择支行，再选择分发人员"),
    ).toBeInTheDocument();
    const select = screen.getByRole("combobox", { name: "选择支行" });
    await waitFor(() => expect(select).toBeEnabled());
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(mockRequest).toHaveBeenNthCalledWith(
      1,
      "/zt/wealth/wmadm/branchTreeInfo",
      {
        method: "POST",
        signal: expect.any(AbortSignal),
        body: JSON.stringify({
          mmOrgCode: "755",
          mmOrgLvl: 70,
          sysId: "cookie-system",
        }),
      },
    );
    fireEvent.mouseDown(select);
    fireEvent.click(await screen.findByText("深圳深湾中心（102）"));
    fireEvent.click(await screen.findByRole("button", { name: /张三/ }));
    expect(mockRequest).toHaveBeenNthCalledWith(
      2,
      "/zt/wealth/wmadm/getUserPostionByBrn",
      {
        method: "POST",
        signal: expect.any(AbortSignal),
        body: JSON.stringify({
          bbkOrgId: "755",
          mmOrgCode: "102",
          mmOrgLvl: 50,
        }),
      },
    );
    fireEvent.mouseDown(select);
    fireEvent.click(await screen.findByText("另一支行（103）"));
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
    expect(mockRequest).toHaveBeenCalledWith(
      "/zt/wealth/wmadm/getUserPostionByBrn",
      {
        method: "POST",
        signal: expect.any(AbortSignal),
        body: JSON.stringify({
          bbkOrgId: "755",
          mmOrgCode: "755480",
          mmOrgLvl: 70,
        }),
      },
    );
  });
});
