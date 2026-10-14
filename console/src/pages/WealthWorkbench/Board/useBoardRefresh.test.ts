import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useBoardRefresh } from "./useBoardRefresh";

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it("仅处理中每5秒查询，持续3分钟暂停，手动刷新不重启轮询", async () => {
  const refresh = vi.fn().mockResolvedValue(undefined);
  const { result, rerender } = renderHook(
    ({ active }) => useBoardRefresh(active, refresh),
    { initialProps: { active: false } },
  );
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(refresh).not.toHaveBeenCalled();
  rerender({ active: true });
  await act(async () => vi.advanceTimersByTimeAsync(4999));
  expect(refresh).not.toHaveBeenCalled();
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(refresh).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTimeAsync(175000));
  expect(result.current.paused).toBe(true);
  const calls = refresh.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(60000));
  expect(refresh).toHaveBeenCalledTimes(calls);
  await act(async () => result.current.manualRefresh());
  expect(refresh).toHaveBeenCalledTimes(calls + 1);
  expect(result.current.statsRefreshVersion).toBe(1);
  await act(async () => vi.advanceTimersByTimeAsync(60000));
  expect(refresh).toHaveBeenCalledTimes(calls + 1);
});

it("慢自动请求和手动刷新共用请求，完成后才安排下一轮", async () => {
  let resolve!: () => void;
  const refresh = vi.fn().mockImplementation(
    () =>
      new Promise<void>((done) => {
        resolve = done;
      }),
  );
  const { result } = renderHook(() => useBoardRefresh(true, refresh));
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  let manual!: Promise<void>;
  act(() => {
    manual = result.current.manualRefresh();
  });
  await act(async () => vi.advanceTimersByTimeAsync(10000));
  expect(refresh).toHaveBeenCalledTimes(1);
  expect(result.current.refreshing).toBe(true);
  await act(async () => {
    resolve();
    await manual;
  });
  await act(async () => vi.advanceTimersByTimeAsync(4999));
  expect(refresh).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(refresh).toHaveBeenCalledTimes(2);
});

it("超时请求自动释放等待，3分钟后不再安排自动查询", async () => {
  const refresh = vi.fn().mockImplementation(() => new Promise<void>(() => {}));
  const { result } = renderHook(() => useBoardRefresh(true, refresh));
  await act(async () => vi.advanceTimersByTimeAsync(180000));
  expect(result.current.paused).toBe(true);
  expect(result.current.refreshing).toBe(false);
  const calls = refresh.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(10000));
  expect(refresh).toHaveBeenCalledTimes(calls);
});

it("完成或卸载时停止轮询，新的处理周期重新计时", async () => {
  const refresh = vi.fn().mockResolvedValue(undefined);
  const { result, rerender, unmount } = renderHook(
    ({ active }) => useBoardRefresh(active, refresh),
    { initialProps: { active: true } },
  );
  await act(async () => vi.advanceTimersByTimeAsync(180000));
  rerender({ active: false });
  expect(result.current.paused).toBe(false);
  rerender({ active: true });
  const calls = refresh.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(refresh).toHaveBeenCalledTimes(calls + 1);
  unmount();
  await act(async () => vi.advanceTimersByTimeAsync(10000));
  expect(refresh).toHaveBeenCalledTimes(calls + 1);
});

it("刷新失败展示失败状态，不触发统计强制刷新，允许重试", async () => {
  const refresh = vi
    .fn()
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValue(undefined);
  const { result } = renderHook(() => useBoardRefresh(false, refresh));
  await act(async () => result.current.manualRefresh());
  expect(result.current.failed).toBe(true);
  expect(result.current.statsRefreshVersion).toBe(0);
  await act(async () => result.current.manualRefresh());
  expect(result.current.failed).toBe(false);
  expect(result.current.statsRefreshVersion).toBe(1);
});

it("规划请求15秒超时后恢复手动刷新，取消旧请求并允许重试", async () => {
  let signal!: AbortSignal;
  const refresh = vi
    .fn()
    .mockImplementationOnce((_, input) => {
      signal = input;
      return new Promise<void>(() => {});
    })
    .mockResolvedValue(undefined);
  const { result } = renderHook(() => useBoardRefresh(false, refresh));
  let pending!: Promise<void>;
  act(() => {
    pending = result.current.manualRefresh();
  });
  await act(async () => vi.advanceTimersByTimeAsync(14999));
  expect(result.current.refreshing).toBe(true);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
    await pending;
  });
  expect(signal.aborted).toBe(true);
  expect(result.current.refreshing).toBe(false);
  expect(result.current.statsRefreshVersion).toBe(0);
  await act(async () => result.current.manualRefresh());
  expect(refresh).toHaveBeenCalledTimes(2);
  expect(result.current.statsRefreshVersion).toBe(1);
});

it("卸载立即取消自动轮询的在途请求并清理所有定时器", async () => {
  let signal!: AbortSignal;
  const refresh = vi.fn().mockImplementation((_, input) => {
    signal = input;
    return new Promise<void>(() => {});
  });
  const { unmount } = renderHook(() => useBoardRefresh(true, refresh));
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  unmount();
  await act(async () => {});
  expect(signal.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
  await act(async () => vi.advanceTimersByTimeAsync(180000));
  expect(refresh).toHaveBeenCalledTimes(1);
});
