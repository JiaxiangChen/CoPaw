import React, {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useRef,
} from "react";

interface AutoPreviewCandidate {
  url: string;
  open: () => void;
}

interface AutoPreviewHtmlContextValue {
  enabled: boolean;
  register: (candidate: AutoPreviewCandidate) => () => void;
}

const noopUnregister = () => undefined;

function previewUrlKey(url: string): string {
  try {
    return decodeURI(new URL(url, window.location.origin).href);
  } catch {
    return url;
  }
}

const AutoPreviewHtmlContext = createContext<AutoPreviewHtmlContextValue>({
  enabled: false,
  register: () => noopUnregister,
});

interface AutoPreviewHtmlProviderProps {
  children: React.ReactNode;
  triggerKey: number;
  onConsumed: () => void;
  targetUrl?: string | null;
  ready?: boolean;
  resolveUrl?: (url: string) => string;
}

export function AutoPreviewHtmlProvider(props: AutoPreviewHtmlProviderProps) {
  const {
    children,
    triggerKey,
    onConsumed,
    targetUrl,
    ready = true,
    resolveUrl,
  } = props;
  const candidatesRef = useRef<AutoPreviewCandidate[]>([]);
  const selectTimerRef = useRef<number | null>(null);
  const expireTimerRef = useRef<number | null>(null);
  const consumedRef = useRef(false);
  const onConsumedRef = useRef(onConsumed);
  const enabled = triggerKey > 0 && ready;

  useLayoutEffect(() => {
    onConsumedRef.current = onConsumed;
  }, [onConsumed]);

  useLayoutEffect(() => {
    consumedRef.current = false;
  }, [triggerKey]);

  const clearSelectTimer = useCallback(() => {
    if (selectTimerRef.current !== null) {
      window.clearTimeout(selectTimerRef.current);
      selectTimerRef.current = null;
    }
  }, []);

  useLayoutEffect(() => {
    candidatesRef.current = [];
    clearSelectTimer();

    if (expireTimerRef.current !== null) {
      window.clearTimeout(expireTimerRef.current);
      expireTimerRef.current = null;
    }

    if (!enabled || consumedRef.current) return;

    expireTimerRef.current = window.setTimeout(() => {
      consumedRef.current = true;
      candidatesRef.current = [];
      onConsumedRef.current();
    }, 5000);

    return () => {
      clearSelectTimer();
      if (expireTimerRef.current !== null) {
        window.clearTimeout(expireTimerRef.current);
        expireTimerRef.current = null;
      }
    };
  }, [clearSelectTimer, enabled, targetUrl, triggerKey]);

  const register = useCallback(
    (candidate: AutoPreviewCandidate) => {
      if (triggerKey <= 0 || !ready || consumedRef.current) return noopUnregister;
      if (
        targetUrl !== undefined &&
        (!targetUrl ||
          previewUrlKey(resolveUrl?.(candidate.url) || candidate.url) !==
            previewUrlKey(resolveUrl?.(targetUrl) || targetUrl))
      ) {
        return noopUnregister;
      }

      candidatesRef.current.push(candidate);
      clearSelectTimer();
      selectTimerRef.current = window.setTimeout(() => {
        if (consumedRef.current) return;

        const latest = candidatesRef.current[candidatesRef.current.length - 1];
        if (!latest) return;
        consumedRef.current = true;
        candidatesRef.current = [];
        latest.open();
        onConsumedRef.current();
      }, 120);

      return () => {
        candidatesRef.current = candidatesRef.current.filter(
          (item) => item !== candidate,
        );
      };
    },
    [clearSelectTimer, ready, targetUrl, triggerKey, resolveUrl],
  );

  const value = useMemo(
    () => ({
      enabled,
      register,
    }),
    [enabled, register],
  );

  return (
    <AutoPreviewHtmlContext.Provider value={value}>
      {children}
    </AutoPreviewHtmlContext.Provider>
  );
}

export function useAutoPreviewHtml() {
  return useContext(AutoPreviewHtmlContext);
}
