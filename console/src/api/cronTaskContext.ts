import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";
import { getUserId } from "@/utils/identity";
import { DEFAULT_SOURCE_ID } from "@/constants/identity";
import type { ManagedCronContext } from "./types/schedulerCron";

/** 捕获当前真实身份，供异步任务操作检查工作区是否已切换。 */
export function currentCronContext(): ManagedCronContext {
  const iframe = useIframeStore.getState();
  const actor = iframe.userId || getUserId();
  return {
    actor_id: actor,
    tenant_id: actor,
    source_id: iframe.source || DEFAULT_SOURCE_ID,
    agent_id: useAgentStore.getState().selectedAgent,
  };
}

export function cronAccessKey(context = currentCronContext()): string {
  const iframe = useIframeStore.getState();
  return JSON.stringify([
    context.actor_id,
    context.tenant_id,
    context.source_id,
    context.agent_id,
    Boolean(iframe.manager),
    Boolean(iframe.isSuperManager),
  ]);
}

/** 目标定位与操作者分开，角色和认证凭据由原请求层实时构建。 */
export function cronContextHeaders(context: ManagedCronContext): HeadersInit {
  return {
    "X-User-Id": context.actor_id,
    "X-Tenant-Id": context.tenant_id,
    "X-Source-Id": context.source_id,
    "X-Agent-Id": context.agent_id,
    "X-Scope-Id": `${context.tenant_id}-${context.source_id}`,
  };
}
