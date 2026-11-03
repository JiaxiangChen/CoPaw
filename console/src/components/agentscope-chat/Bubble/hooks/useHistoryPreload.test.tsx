import { act, cleanup, fireEvent, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import useHistoryPreload from "./useHistoryPreload";

let frames: Map<number, FrameRequestCallback>;
let nextFrameId: number;
async function frame() {
  const pending = [...frames.values()];
  frames.clear();
  await act(async () => pending.forEach((callback) => callback(0)));
}
async function settle() {
  await frame();
  await frame();
}
function setup(
  onNearStart = vi.fn().mockResolvedValue(true),
  options: Partial<Parameters<typeof useHistoryPreload>[0]> = {},
  scrollHeight = 1000,
) {
  const element = document.createElement("div");
  Object.defineProperties(element, {
    clientHeight: { configurable: true, value: 400 },
    scrollHeight: { configurable: true, value: scrollHeight },
    scrollTop: { configurable: true, value: 0, writable: true },
  });
  const hook = renderHook(
    ({ disabled, resetKey, scrollElement }) =>
      useHistoryPreload({
        scrollElement,
        onNearStart,
        disabled,
        resetKey,
        ...options,
      }),
    {
      initialProps: {
        disabled: false,
        resetKey: "chat-1",
        scrollElement: element,
      },
    },
  );
  const scroll = async (top: number) => {
    await act(async () => {
      element.scrollTop = top;
      fireEvent.scroll(element);
    });
  };
  return { element, onNearStart, hook, scroll };
}

describe("useHistoryPreload", () => {
  beforeEach(() => {
    frames = new Map();
    nextFrameId = 0;
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      frames.set(++nextFrameId, callback);
      return nextFrameId;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("does not load a scrollable list on mount", async () => {
    const { onNearStart } = setup();
    await settle();
    expect(onNearStart).not.toHaveBeenCalled();
  });

  it("automatically checks history when the list is too short to scroll", async () => {
    const { onNearStart } = setup(undefined, {}, 300);
    await act(async () => {});
    expect(onNearStart).toHaveBeenCalledTimes(1);
  });

  it("stops initial filling as soon as the content becomes scrollable", async () => {
    const { element, onNearStart } = setup(undefined, {}, 300);
    await act(async () => {});
    Object.defineProperty(element, "scrollHeight", { value: 500 });
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(1);
  });

  it("continues initial filling after finding a user while the viewport remains short", async () => {
    const onNearStart = vi.fn().mockResolvedValue({
      progressed: true,
      foundUser: true,
      archive: false,
    });
    const { element } = setup(onNearStart, {}, 300);
    await act(async () => {});
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(2);
    Object.defineProperty(element, "scrollHeight", { value: 500 });
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(2);
  });

  it("stops explicit history advancement when a user is found", async () => {
    const onNearStart = vi.fn().mockResolvedValue({
      progressed: true,
      foundUser: true,
      archive: false,
    });
    const { scroll } = setup(onNearStart);
    await scroll(-380);
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(1);
  });

  it("bounds initial filling when each short batch contains a user", async () => {
    const onNearStart = vi.fn().mockResolvedValue({
      progressed: true,
      foundUser: true,
      archive: false,
    });
    setup(onNearStart, {}, 300);
    await act(async () => {});
    await settle();
    await settle();
    await settle();
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(3);
  });

  it("continues at the top after layout settles without requiring a downward scroll", async () => {
    const { onNearStart, scroll } = setup();
    await scroll(-380);
    expect(onNearStart).toHaveBeenCalledTimes(1);
    await scroll(-600);
    expect(onNearStart).toHaveBeenCalledTimes(1);
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(2);
  });

  it("holds the lock until anchor recovery finishes even if its RAF runs later", async () => {
    let recoveryPending = true;
    const { onNearStart, hook, scroll } = setup(undefined, {
      isAnchorRecoveryPending: () => recoveryPending,
    });
    await scroll(-380);
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(1);
    expect(hook.result.current.isAdvancing).toBe(true);
    await scroll(-600);
    expect(onNearStart).toHaveBeenCalledTimes(1);
    recoveryPending = false;
    await frame();
    expect(onNearStart).toHaveBeenCalledTimes(2);
  });

  it("rechecks changed content geometry and handles a jump directly to the new top", async () => {
    const { element, onNearStart, scroll } = setup();
    await scroll(-380);
    Object.defineProperty(element, "scrollHeight", {
      configurable: true,
      value: 1600,
    });
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(1);
    await scroll(-1200);
    expect(onNearStart).toHaveBeenCalledTimes(2);
  });

  it("bounds automatic progression to three steps and permits a later upward scroll", async () => {
    const { onNearStart, scroll } = setup();
    await scroll(-380);
    await settle();
    await settle();
    await settle();
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(3);
    await scroll(-600);
    expect(onNearStart).toHaveBeenCalledTimes(4);
  });

  it("stops when the callback reports no progress", async () => {
    const { onNearStart, scroll } = setup(vi.fn().mockResolvedValue(false));
    await scroll(-600);
    await settle();
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(1);
  });

  it("does not continue after the reader scrolls downward during a request", async () => {
    let resolve!: (value: boolean) => void;
    const { onNearStart, scroll } = setup(
      vi.fn(
        () =>
          new Promise<boolean>((done) => {
            resolve = done;
          }),
      ),
    );
    await scroll(-600);
    await scroll(-500);
    await act(async () => resolve(true));
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(1);
  });

  it("stops for keyboard or scrollbar downward movement between layout frames", async () => {
    const { onNearStart, scroll } = setup();
    await scroll(-600);
    await frame();
    await scroll(-500);
    await frame();
    expect(onNearStart).toHaveBeenCalledTimes(1);
  });

  it("stops when disabled after a request and allows explicit continuation for short lists", async () => {
    const { element, onNearStart, hook, scroll } = setup();
    await scroll(-600);
    hook.rerender({
      disabled: true,
      resetKey: "chat-1",
      scrollElement: element,
    });
    await settle();
    expect(onNearStart).toHaveBeenCalledTimes(1);
    hook.rerender({
      disabled: false,
      resetKey: "chat-1",
      scrollElement: element,
    });
    await act(async () => hook.result.current.loadMore());
    expect(onNearStart).toHaveBeenCalledTimes(2);
  });

  it("ignores an old request after the session changes without unlocking the new request", async () => {
    const resolvers: Array<(value: boolean) => void> = [];
    const { element, onNearStart, hook, scroll } = setup(
      vi.fn(() => new Promise<boolean>((resolve) => resolvers.push(resolve))),
    );
    await scroll(-380);
    hook.rerender({
      disabled: false,
      resetKey: "chat-2",
      scrollElement: element,
    });
    await scroll(-600);
    await act(async () => resolvers[0](true));
    await settle();
    await scroll(-580);
    await scroll(-600);
    expect(onNearStart).toHaveBeenCalledTimes(2);
    hook.unmount();
    await act(async () => resolvers[1](true));
    expect(frames.size).toBe(0);
  });
});
