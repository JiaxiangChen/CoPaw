import { useEffect, useState } from "react";

/** 身份、筛选或批次变化后立即隐藏旧数据，并取消后续游标请求。 */
export function useBoardResource<T>(load: (signal: AbortSignal) => Promise<T>) {
  const [state, setState] = useState<{
    load?: typeof load;
    value?: T;
    error?: string;
    loading: boolean;
  }>({ loading: true });
  useEffect(() => {
    const controller = new AbortController();
    setState({ load, loading: true });
    load(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted)
          setState({ load, value, loading: false });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setState({
            load,
            error: error instanceof Error ? error.message : "加载失败",
            loading: false,
          });
      });
    return () => controller.abort();
  }, [load]);
  return state.load === load
    ? state
    : { loading: true, value: undefined, error: undefined };
}
