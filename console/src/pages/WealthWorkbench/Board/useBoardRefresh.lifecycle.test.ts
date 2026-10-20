import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { request } from "../../../api/request";
import { useWealthStore } from "../store";
import type { Plan } from "../types";
import { useBoardRefresh } from "./useBoardRefresh";

vi.mock("../../../api/request", () => ({ request: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("离开看板立即取消旧刷新，即使旧响应晚返回也不覆盖后续规划", async () => {
  let resolve!: (value: unknown) => void;
  vi.mocked(request).mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string, options?: RequestInit) =>
        new Response(
          JSON.stringify({ code: "200", data: await request(path, options) }),
        ),
    ),
  );
  const action = useWealthStore.getState().refreshPlans;
  const { result, unmount } = renderHook(() => useBoardRefresh(false, action));
  let pending!: Promise<void>;
  act(() => {
    pending = result.current.manualRefresh();
  });
  const signal = vi.mocked(request).mock.calls[0][1]?.signal;
  unmount();
  expect(signal?.aborted).toBe(true);
  // 不等待底层请求响应，离开后立即释放等待。
  await act(async () => pending);
  const latest = [{ id: "new-plan" }] as Plan[];
  useWealthStore.setState({ plans: latest });
  await act(async () => {
    resolve({ items: [] });
  });
  expect(useWealthStore.getState().plans).toBe(latest);
});
