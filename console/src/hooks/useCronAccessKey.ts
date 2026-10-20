import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";
import { cronAccessKey } from "@/api/cronTaskContext";

/** 订阅身份和真实角色；凭据刷新不重建业务操作身份。 */
export function useCronAccessKey(): string {
  useAgentStore((state) => state.selectedAgent);
  useIframeStore((state) =>
    JSON.stringify([
      state.userId,
      state.source,
      state.manager,
      state.isSuperManager,
    ]),
  );
  return cronAccessKey();
}
