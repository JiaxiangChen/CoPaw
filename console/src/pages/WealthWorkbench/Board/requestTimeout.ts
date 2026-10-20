const BOARD_REQUEST_TIMEOUT_MS = 60_000;

/** 超时或外部取消都中止底层请求；即使取消未及时响应，也释放等待。 */
export async function withBoardRequestTimeout<T>(
  request: (signal: AbortSignal) => Promise<T>,
  signal?: AbortSignal,
): Promise<T> {
  signal?.throwIfAborted();
  const controller = new AbortController();
  let rejectCancellation!: (reason: unknown) => void;
  const cancellation = new Promise<never>((_, reject) => {
    rejectCancellation = reject;
  });
  const onAbort = () => rejectCancellation(controller.signal.reason);
  const cancel = () => controller.abort(signal?.reason);
  controller.signal.addEventListener("abort", onAbort, { once: true });
  signal?.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(() => {
    controller.abort(new DOMException("看板查询超时", "TimeoutError"));
  }, BOARD_REQUEST_TIMEOUT_MS);
  try {
    return await Promise.race([request(controller.signal), cancellation]);
  } finally {
    clearTimeout(timer);
    controller.signal.removeEventListener("abort", onAbort);
    signal?.removeEventListener("abort", cancel);
  }
}
