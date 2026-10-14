import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { withBoardRequestTimeout } from "./requestTimeout";
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());
it("15秒边界取消请求，底层忽略取消时仍释放等待", async () => {
  let signal!: AbortSignal;
  const pending = withBoardRequestTimeout((input) => {
    signal = input;
    return new Promise(() => {});
  });
  const rejection = expect(pending).rejects.toMatchObject({
    name: "TimeoutError",
  });
  await vi.advanceTimersByTimeAsync(14999);
  expect(signal.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  await rejection;
  expect(signal.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});
it("正常完成后清理超时定时器，不取消成功请求", async () => {
  let signal!: AbortSignal;
  await expect(
    withBoardRequestTimeout(async (input) => {
      signal = input;
      return 12;
    }),
  ).resolves.toBe(12);
  expect(vi.getTimerCount()).toBe(0);
  await vi.advanceTimersByTimeAsync(15000);
  expect(signal.aborted).toBe(false);
});

it("外部取消立即释放等待和定时器，底层忽略取消也不阻塞", async () => {
  const controller = new AbortController();
  let signal!: AbortSignal;
  const pending = withBoardRequestTimeout((input) => {
    signal = input;
    return new Promise(() => {});
  }, controller.signal);
  const rejection = expect(pending).rejects.toMatchObject({
    name: "AbortError",
  });
  controller.abort();
  await rejection;
  expect(signal.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});
it("已取消的外部信号不发起请求", async () => {
  const controller = new AbortController();
  controller.abort();
  const request = vi.fn();
  await expect(
    withBoardRequestTimeout(request, controller.signal),
  ).rejects.toMatchObject({ name: "AbortError" });
  expect(request).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});
it("成功后解除外部取消监听，不误取消已完成请求", async () => {
  const controller = new AbortController();
  let signal!: AbortSignal;
  await withBoardRequestTimeout(async (input) => {
    signal = input;
    return 12;
  }, controller.signal);
  controller.abort();
  expect(signal.aborted).toBe(false);
});
