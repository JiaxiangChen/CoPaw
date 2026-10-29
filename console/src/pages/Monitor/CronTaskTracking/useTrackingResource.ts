import { useEffect, useState } from "react";

export function useTrackingResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  revision = 0,
) {
  const [state, setState] = useState<{
    load?: typeof load;
    revision?: number;
    value?: T;
    error?: string;
    loading: boolean;
  }>({ loading: true });
  useEffect(() => {
    const controller = new AbortController();
    setState({ load, revision, loading: true });
    load(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted)
          setState({ load, revision, value, loading: false });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setState({
            load,
            revision,
            error: error instanceof Error ? error.message : "加载失败",
            loading: false,
          });
      });
    return () => controller.abort();
  }, [load, revision]);
  return state.load === load && state.revision === revision
    ? state
    : { loading: true, value: undefined, error: undefined };
}
