import { create } from "zustand";
import { BBK_ID_MAP } from "../../constants/bbk";
import { useIframeStore } from "../../stores/iframeStore";

interface OperationsState {
  enabled: boolean;
  branchId: string | null;
  identity: string;
}

/** 仅工作台内存，不写入 Cookie、共享身份或浏览器存储。 */
export const useWealthOperations = create<OperationsState>(() => ({
  enabled: false,
  branchId: null,
  identity: "",
}));

export function operationsIdentity(): string {
  const { userId, bbk, positionId, source, orgCode, orgLvl, sysId } =
    useIframeStore.getState();
  return JSON.stringify([
    userId,
    bbk,
    positionId,
    source,
    orgCode,
    orgLvl,
    sysId,
  ]);
}

export function canUseOperations(): boolean {
  const { userId, bbk } = useIframeStore.getState();
  if (!userId || !bbk) return false;
  // 运行时配置只控制界面入口；缺失/错误时拒绝开放，服务端仍需独立授权。
  const config = window.__env__?.operationsUsersByBranch;
  if (!config || typeof config !== "object" || Array.isArray(config))
    return false;
  const users = config[bbk];
  return Array.isArray(users) && users.includes(userId);
}

export function getOperationsBranches() {
  return BBK_ID_MAP.filter((branch) => branch.value !== "100");
}

export function isOperationsMode(): boolean {
  const state = useWealthOperations.getState();
  return (
    state.enabled &&
    canUseOperations() &&
    state.identity === operationsIdentity()
  );
}

/** 只供财富业务入参使用，统一鉴权继续读取真实 iframe 身份。 */
export function getWorkbenchContext() {
  const context = useIframeStore.getState();
  if (!isOperationsMode()) return context;
  const { branchId } = useWealthOperations.getState();
  if (!branchId) throw new Error("请选择运维分行");
  return {
    ...context,
    bbk: branchId,
    orgCode: branchId,
    subBranchId: null,
    positionId: "RB0301",
  };
}
