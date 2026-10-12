import { beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../../api/request";
import { useIframeStore } from "../../stores/iframeStore";
import { fetchDistributeTargets } from "./distributeTargets";

vi.mock("../../api/request", () => ({ request: vi.fn() }));
const mockRequest = vi.mocked(request);

beforeEach(() => {
  mockRequest.mockReset();
  useIframeStore.getState().clearContext();
  useIframeStore.getState().setContext({
    bbk: "755",
    orgCode: "755480",
    orgLvl: "70",
  });
});

describe("机构岗位人员分发目标", () => {
  it("按登录机构查询，使用 sapID 并保留返回的不同岗位", async () => {
    mockRequest.mockResolvedValue({
      code: "success",
      errMsg: null,
      data: [
        {
          userID: "00080008",
          sapID: "00000000",
          userName: "邓XX",
          branchID: "100",
          branchName: "XXX全行",
          subBranchName: null,
          postionId: "RB0101",
        },
        {
          sapID: "00000001",
          userName: "李XX",
          branchName: "XXX全行",
          subBranchName: "某支行",
          postionId: "RB1101",
        },
      ],
    });
    expect(await fetchDistributeTargets()).toEqual([
      { sapId: "00000000", name: "邓XX", orgName: "XXX全行" },
      { sapId: "00000001", name: "李XX", orgName: "某支行" },
    ]);
    expect(mockRequest).toHaveBeenCalledWith("/user-info/positions/by-branch", {
      method: "POST",
      body: JSON.stringify({
        bbkOrgId: "755",
        mOrgCode: "755480",
        mOrgLvl: 70,
      }),
    });
  });

  it.each([null, "", " ", "NaN", "70.5"])(
    "机构级别 %s 无效时不发请求",
    async (orgLvl) => {
      useIframeStore.getState().setContext({ orgLvl });
      expect(await fetchDistributeTargets()).toEqual([]);
      expect(mockRequest).not.toHaveBeenCalled();
    },
  );

  it.each(["bbk", "orgCode"] as const)("缺少 %s 时不发请求", async (key) => {
    useIframeStore.getState().setContext({ [key]: null });
    expect(await fetchDistributeTargets()).toEqual([]);
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("业务失败和空列表返回空目标", async () => {
    mockRequest.mockResolvedValueOnce({ code: "failed", data: [] });
    expect(await fetchDistributeTargets()).toEqual([]);
    mockRequest.mockResolvedValueOnce({ code: "success", data: null });
    expect(await fetchDistributeTargets()).toEqual([]);
  });

  it("请求失败时返回空目标", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    mockRequest.mockRejectedValue(new Error("offline"));
    expect(await fetchDistributeTargets()).toEqual([]);
    warn.mockRestore();
  });
});
