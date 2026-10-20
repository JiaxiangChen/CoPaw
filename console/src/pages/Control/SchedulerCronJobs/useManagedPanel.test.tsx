import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { useManagedPanel } from "./useManagedPanel";
const idle = () => false;

it("异步受理结果替换视图后，先前查询迟到不能覆盖已受理操作", async () => {
  let resolve!: (value: string) => void;
  const load = () =>
    new Promise<string>((done) => {
      resolve = done;
    });
  const { result } = renderHook(() => useManagedPanel(load, idle));
  act(() => result.current.replaceValue("accepted-operation"));
  await act(async () => resolve("old-empty-query"));
  expect(result.current.value).toBe("accepted-operation");
  expect(result.current.loading).toBe(false);
});

it("新查询已返回后，旧查询迟到不能覆盖弹窗进度", async () => {
  let resolve!: (value: string) => void;
  const oldLoad = () =>
    new Promise<string>((done) => {
      resolve = done;
    });
  const newLoad = vi.fn().mockResolvedValue("new");
  const { result, rerender } = renderHook(
    ({ load }) => useManagedPanel(load, idle),
    { initialProps: { load: oldLoad } },
  );
  rerender({ load: newLoad });
  await waitFor(() => expect(result.current.value).toBe("new"));
  await act(async () => {
    resolve("old");
  });
  expect(result.current.value).toBe("new");
});

it("刷新失败显示错误但保留已确认的结果", async () => {
  const load = vi.fn().mockResolvedValue("known");
  const { result } = renderHook(() => useManagedPanel(load, idle));
  await waitFor(() => expect(result.current.value).toBe("known"));
  load.mockRejectedValueOnce(new Error("unavailable"));
  await act(async () => {
    await result.current.reload();
  });
  expect(result.current.value).toBe("known");
  expect(result.current.error).toBe("unavailable");
});
