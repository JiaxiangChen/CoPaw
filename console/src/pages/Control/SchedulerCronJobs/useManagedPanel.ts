import { useCallback, useEffect, useRef, useState } from "react";
import { failureMessage } from "./helpers";

/** 弹窗关闭或身份变化即停止轮询，迟到响应不更新其他工作区。 */
export function useManagedPanel<T>(
  load: () => Promise<T>,
  pending: (value: T) => boolean,
) {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const alive = useRef(false);
  const serial = useRef(0);
  /** 写请求已确认的新视图作废此前在途查询，避免迟到空结果覆盖受理进度。 */
  const replaceValue = useCallback((next: T) => {
    ++serial.current;
    setValue(next);
    setError(null);
    setLoading(false);
  }, []);
  const reload = useCallback(async () => {
    const current = ++serial.current;
    setLoading(true);
    try {
      const result = await load();
      if (alive.current && current === serial.current) {
        setValue(result);
        setError(null);
      }
    } catch (failure) {
      if (alive.current && current === serial.current)
        setError(failureMessage(failure));
    } finally {
      if (alive.current && current === serial.current) setLoading(false);
    }
  }, [load]);
  useEffect(() => {
    alive.current = true;
    void reload();
    return () => {
      alive.current = false;
    };
  }, [reload]);
  useEffect(() => {
    if (!value || !pending(value) || loading || error) return;
    const timer = window.setTimeout(() => void reload(), 3000);
    return () => window.clearTimeout(timer);
  }, [value, loading, error, pending, reload]);
  return {
    value,
    setValue,
    replaceValue,
    error,
    setError,
    loading,
    reload,
    alive,
  };
}
