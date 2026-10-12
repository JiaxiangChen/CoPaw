/**
 * 智能财富工作台 —— 分发目标用户池
 *
 * 行长/中台发布规划前选择分发对象，通过后端代理查询机构岗位人员。
 * 机构参数取自 iframeStore 中由 cookie 初始化的 bbk/orgCode/orgLvl。
 *
 * 机构参数缺失或接口查询失败时返回空列表，
 * 由页面展示无数据态，不做假数据兜底。
 */
import { request } from "../../api/request";
import { useIframeStore } from "../../stores/iframeStore";
import type { DistributeTarget } from "./types";

export interface DistributeBranch {
  mOrgCode: string;
  mOrgName: string;
  mOrgLvl: number;
  leaf: boolean;
}

/** 按当前登录分行查询可选择的下属机构。 */
export async function fetchDistributeBranches(
  branch?: DistributeBranch,
): Promise<DistributeBranch[] | null> {
  const { bbk, sysId, orgLvl } = useIframeStore.getState();
  const mOrgCode = branch?.mOrgCode ?? bbk;
  const level = branch ? String(branch.mOrgLvl) : orgLvl;
  if (!mOrgCode || !sysId || !level?.trim()) return branch ? null : [];
  const mOrgLvl = Number(level);
  if (!Number.isInteger(mOrgLvl) || mOrgLvl < 0) return branch ? null : [];
  try {
    return await request<DistributeBranch[]>("/user-info/branches/by-branch", {
      method: "POST",
      body: JSON.stringify({
        mOrgCode,
        mOrgLvl,
        sysId,
      }),
    });
  } catch (error) {
    console.warn("[Wealth] 分发支行查询失败", error);
    return null;
  }
}

interface UserPositionResponse {
  code: string;
  errMsg: string | null;
  data: Array<{
    sapID: string;
    userName: string;
    branchName: string | null;
    subBranchName: string | null;
  }> | null;
}

/**
 * 拉取当前登录人所在分行的分发目标用户池。
 * 查不到身份或接口失败时返回空列表，由页面展示无数据态。
 */
export async function fetchDistributeTargets(
  branch?: DistributeBranch,
): Promise<DistributeTarget[]> {
  const { bbk, orgCode, orgLvl } = useIframeStore.getState();
  const mOrgCode = branch?.mOrgCode ?? orgCode;
  const level = branch ? String(branch.mOrgLvl) : orgLvl;
  if (!bbk || !mOrgCode || !level?.trim()) {
    return [];
  }
  const mOrgLvl = Number(level);
  if (!Number.isInteger(mOrgLvl) || mOrgLvl < 0) return [];
  try {
    const response = await request<UserPositionResponse>(
      "/user-info/positions/by-branch",
      {
        method: "POST",
        body: JSON.stringify({ bbkOrgId: bbk, mOrgCode, mOrgLvl }),
      },
    );
    if (response.code !== "success") return [];
    return (response.data ?? [])
      .filter((user) => user.sapID)
      .map((user) => ({
        sapId: user.sapID,
        name: user.userName || user.sapID,
        orgName: user.subBranchName || user.branchName || "",
      }));
  } catch (error) {
    console.warn("[Wealth] 分发目标用户池查询失败，按无数据处理", error);
    return [];
  }
}
