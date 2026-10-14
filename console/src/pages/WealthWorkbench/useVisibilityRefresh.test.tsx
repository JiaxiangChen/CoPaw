import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useVisibilityRefresh } from "./useVisibilityRefresh";

const refreshTaskLists = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));
vi.mock("./store", () => ({
  useWealthStore: { getState: () => ({ refreshTaskLists }) },
}));

function changeVisibility(value: DocumentVisibilityState) {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue(value);
  act(() => document.dispatchEvent(new Event("visibilitychange")));
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("财富工作台标签页切回刷新", () => {
  it("首次挂载和切走不刷新，每次从隐藏切回可见时刷新", () => {
    renderHook(useVisibilityRefresh);
    changeVisibility("visible");
    changeVisibility("hidden");
    expect(refreshTaskLists).not.toHaveBeenCalled();
    changeVisibility("visible");
    expect(refreshTaskLists).toHaveBeenCalledTimes(1);
    changeVisibility("visible");
    expect(refreshTaskLists).toHaveBeenCalledTimes(1);
    changeVisibility("hidden");
    changeVisibility("visible");
    expect(refreshTaskLists).toHaveBeenCalledTimes(2);
  });

  it("在后台挂载时，首次切回也刷新；卸载后不再监听", () => {
    changeVisibility("hidden");
    const { unmount } = renderHook(useVisibilityRefresh);
    changeVisibility("visible");
    expect(refreshTaskLists).toHaveBeenCalledTimes(1);
    unmount();
    changeVisibility("hidden");
    changeVisibility("visible");
    expect(refreshTaskLists).toHaveBeenCalledTimes(1);
  });
});
