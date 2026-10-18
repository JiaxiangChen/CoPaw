import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { CronJobSpecOutput } from "@/api/types";
import {
  listTaskSources,
  mergeTaskSources,
} from "@/api/modules/cronTaskSources";
import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";
import { DEFAULT_SOURCE_ID } from "@/constants/identity";
import { getUserId } from "@/utils/identity";

/** 旧列表在前、新列表在后；失败来源保留，身份变化立即隔离旧展示与写回。 */
export function useChatTaskSources() {
  const user = useIframeStore((state) => state.userId);
  const source = useIframeStore((state) => state.source);
  const manager = useIframeStore((state) => state.manager);
  const admin = useIframeStore((state) => state.isSuperManager);
  const agent = useAgentStore((state) => state.selectedAgent);
  const actor = user || getUserId();
  const context = useMemo(
    () => ({
      actor_id: actor,
      tenant_id: actor,
      source_id: source || DEFAULT_SOURCE_ID,
      agent_id: agent,
    }),
    [actor, source, agent],
  );
  const scopeKey = JSON.stringify([
    context.actor_id,
    context.tenant_id,
    context.source_id,
    context.agent_id,
    Boolean(manager),
    Boolean(admin),
  ]);
  const currentKey = useRef(scopeKey);
  currentKey.current = scopeKey;
  const mounted = useRef(false);
  const serial = useRef(0);
  const [snapshot, setSnapshot] = useState<{
    key: string;
    jobs: CronJobSpecOutput[];
    error: string | null;
  }>({ key: scopeKey, jobs: [], error: null });
  const setJobs: Dispatch<SetStateAction<CronJobSpecOutput[]>> = useCallback(
    (update) => {
      if (!mounted.current || currentKey.current !== scopeKey) return;
      setSnapshot((previous) => {
        const jobs = previous.key === scopeKey ? previous.jobs : [];
        return {
          key: scopeKey,
          error: previous.key === scopeKey ? previous.error : null,
          jobs: typeof update === "function" ? update(jobs) : update,
        };
      });
    },
    [scopeKey],
  );
  const refreshJobs = useCallback(
    async (signal?: AbortSignal) => {
      if (!mounted.current || currentKey.current !== scopeKey) return;
      const request = ++serial.current;
      const sources = await listTaskSources(context, signal);
      if (
        !mounted.current ||
        currentKey.current !== scopeKey ||
        serial.current !== request ||
        signal?.aborted
      )
        return;
      setSnapshot((previous) => ({
        key: scopeKey,
        jobs: mergeTaskSources(
          previous.key === scopeKey ? previous.jobs : [],
          sources,
        ),
        error: sources.errors.length ? sources.errors.join("；") : null,
      }));
    },
    [context, scopeKey],
  );
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void refreshJobs(controller.signal);
    return () => {
      mounted.current = false;
      controller.abort();
    };
  }, [refreshJobs]);
  return {
    jobs: snapshot.key === scopeKey ? snapshot.jobs : [],
    error: snapshot.key === scopeKey ? snapshot.error : null,
    setJobs,
    refreshJobs,
    context,
    scopeKey,
  };
}
