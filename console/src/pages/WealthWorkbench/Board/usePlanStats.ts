import { useEffect, useMemo, useState } from "react";
import { fetchSkillStats } from "../api";
import type { Plan, SkillStat } from "../types";
import { withBoardRequestTimeout } from "./requestTimeout";

const STATS_CACHE_TTL_MS = 30_000;
type Stats = Record<string, SkillStat> | null;
interface CacheEntry {
  promise?: Promise<void>;
  data?: Stats;
  expiresAt: number;
  revision: number;
  failed: boolean;
}

function isActive(plan: Plan): boolean {
  return (
    plan.publishStatus === "publishing" ||
    plan.status.includes("发布中") ||
    plan.status.includes("分发中")
  );
}

/** 按账户和查询参数缓存；轮询复用，完成事件和有效期触发后台更新。 */
export function usePlanStats(
  visiblePlans: Plan[],
  plans: Plan[],
  accountId: string,
  refreshVersion = 0,
) {
  const cache = useMemo(
    () => ({
      accountId,
      entries: new Map<string, CacheEntry>(),
      activeIds: new Set<string>(),
      revision: 0,
      refreshVersion: 0,
    }),
    [accountId],
  );
  const key = JSON.stringify(
    [...new Set(visiblePlans.map((p) => p.id))].sort(),
  );
  const selection = useMemo(
    () => ({ visiblePlans, checkedAt: Date.now() }),
    [visiblePlans],
  );
  const [, updateStats] = useState(0);

  useEffect(() => {
    const completed = plans.some(
      (plan) => cache.activeIds.has(plan.id) && !isActive(plan),
    );
    cache.activeIds = new Set(plans.filter(isActive).map((plan) => plan.id));
    if (completed || cache.refreshVersion !== refreshVersion)
      cache.revision += 1;
    cache.refreshVersion = refreshVersion;
    if (key === "[]") return;
    let alive = true;
    let entry = cache.entries.get(key);
    if (!entry) {
      entry = { expiresAt: 0, revision: -1, failed: false };
      cache.entries.set(key, entry);
    }
    const pending = entry;
    const needsRefresh =
      pending.revision < cache.revision ||
      selection.checkedAt >= pending.expiresAt;
    if (!pending.promise && needsRefresh) {
      pending.promise = (async () => {
        do {
          const revision = cache.revision;
          const data = await withBoardRequestTimeout((signal) =>
            fetchSkillStats(JSON.parse(key), signal),
          ).catch(() => null);
          pending.failed = data === null;
          if (data !== null || pending.data === undefined) pending.data = data;
          pending.expiresAt = Date.now() + STATS_CACHE_TTL_MS;
          pending.revision = revision;
          // 完成事件发生在请求途中时，再查一次完成后的统计。
        } while (pending.revision < cache.revision);
      })().finally(() => {
        pending.promise = undefined;
      });
      updateStats((version) => version + 1);
    }
    if (pending.promise) {
      void pending.promise.then(() => {
        if (alive) updateStats((version) => version + 1);
      });
    }
    return () => {
      alive = false;
    };
  }, [cache, key, selection, plans, refreshVersion]);

  const entry = cache.entries.get(key);
  const needsRefresh =
    key !== "[]" &&
    (!entry ||
      entry.revision < cache.revision ||
      selection.checkedAt >= entry.expiresAt);
  const fetching = Boolean(entry?.promise) || needsRefresh;
  const sceneStats = key === "[]" ? {} : entry?.data ?? null;
  return {
    sceneStats,
    statsLoading: fetching && sceneStats === null,
    statsUpdating: fetching && sceneStats !== null,
    statsFailed: Boolean(entry?.failed) && !fetching,
  };
}
