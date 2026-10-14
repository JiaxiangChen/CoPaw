import { useCallback, useEffect, useRef, useState } from "react";
import { withBoardRequestTimeout } from "./requestTimeout";

const POLL_INTERVAL_MS = 5_000;
const POLL_DURATION_MS = 3 * 60_000;

/** 每次请求结束后再安排查询；超时暂停只影响自动查询。 */
export function useBoardRefresh(
  hasActive: boolean,
  refreshPlans: (throwOnError?: boolean, signal?: AbortSignal) => Promise<void>,
) {
  const pending = useRef<Promise<boolean> | null>(null);
  const requestController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const [refreshing, setRefreshing] = useState(false);
  const [paused, setPaused] = useState(false);
  const [failed, setFailed] = useState(false);
  const [statsRefreshVersion, setStatsRefreshVersion] = useState(0);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      requestController.current?.abort();
    };
  }, []);

  const refresh = useCallback(() => {
    if (pending.current) return pending.current;
    setRefreshing(true);
    setFailed(false);
    const controller = new AbortController();
    requestController.current = controller;
    pending.current = withBoardRequestTimeout(
      (signal) => refreshPlans(true, signal),
      controller.signal,
    )
      .then(
        () => true,
        () => {
          if (mounted.current) setFailed(true);
          return false;
        },
      )
      .finally(() => {
        pending.current = null;
        requestController.current = null;
        if (mounted.current) setRefreshing(false);
      });
    return pending.current;
  }, [refreshPlans]);

  const manualRefresh = useCallback(async () => {
    const success = await refresh();
    if (success && mounted.current)
      setStatsRefreshVersion((version) => version + 1);
  }, [refresh]);

  useEffect(() => {
    setPaused(false);
    if (!hasActive) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh();
      if (!stopped) timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
    };
    timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
    const deadline = setTimeout(() => {
      stopped = true;
      clearTimeout(timer);
      setPaused(true);
    }, POLL_DURATION_MS);
    return () => {
      stopped = true;
      clearTimeout(timer);
      clearTimeout(deadline);
    };
  }, [hasActive, refresh]);

  return { refreshing, paused, failed, statsRefreshVersion, manualRefresh };
}
