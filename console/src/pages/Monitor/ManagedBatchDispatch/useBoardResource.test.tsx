import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useBoardResource } from "./useBoardResource";
afterEach(cleanup);
it("切换上下文后忽略旧响应并取消旧请求", async () => {
  let resolve!: (value: string) => void;
  const first = vi.fn<(signal: AbortSignal) => Promise<string>>(
    () =>
      new Promise<string>((next) => {
        resolve = next;
      }),
  );
  const second = vi.fn<(signal: AbortSignal) => Promise<string>>(
    async () => "new-source",
  );
  const view = renderHook(({ load }) => useBoardResource(load), {
    initialProps: { load: first },
  });
  view.rerender({ load: second });
  await waitFor(() => expect(view.result.current.value).toBe("new-source"));
  expect(first.mock.calls[0][0].aborted).toBe(true);
  await act(async () => resolve("old-source"));
  expect(view.result.current.value).toBe("new-source");
  view.unmount();
  expect(second.mock.calls[0][0].aborted).toBe(true);
});
