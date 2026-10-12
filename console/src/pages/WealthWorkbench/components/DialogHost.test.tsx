import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useWealthStore } from "../store";
import { DialogHost } from "./DialogHost";

const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;
const originalShowPopover = HTMLElement.prototype.showPopover;
const originalHidePopover = HTMLElement.prototype.hidePopover;

describe("DialogHost feedback", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
      this.setAttribute("open", "");
    };
    HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
      this.removeAttribute("open");
    };
    HTMLElement.prototype.showPopover = function (this: HTMLElement) {
      this.hidden = false;
      this.style.display = "block";
      this.dataset.popoverOpen = "true";
    };
    HTMLElement.prototype.hidePopover = function (this: HTMLElement) {
      this.hidden = true;
      this.style.display = "none";
      delete this.dataset.popoverOpen;
    };
    useWealthStore.setState({
      dialog: null,
      acting: false,
      toastText: "",
      toastSeq: 0,
    });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
    HTMLDialogElement.prototype.showModal = originalShowModal;
    HTMLDialogElement.prototype.close = originalClose;
    HTMLElement.prototype.showPopover = originalShowPopover;
    HTMLElement.prototype.hidePopover = originalHidePopover;
  });

  it("发布失败时提示位于弹窗内，关闭弹窗后移回页面且不重置计时", () => {
    const { container } = render(<DialogHost />);
    act(() =>
      useWealthStore.getState().openDialog({
        title: "发布工作规划",
        body: "确认配置",
        buttons: [],
      }),
    );
    act(() => useWealthStore.getState().toast("发布失败：任务周期重叠"));

    const dialog = container.querySelector("dialog")!;
    expect(dialog).toContainElement(screen.getByRole("status"));
    expect(screen.getByRole("status")).toHaveAttribute("popover", "manual");
    expect(screen.getByRole("status")).toHaveAttribute(
      "data-popover-open",
      "true",
    );
    expect(screen.getByRole("status")).toHaveTextContent("任务周期重叠");
    act(() => vi.advanceTimersByTime(2000));
    act(() => useWealthStore.getState().closeDialog());
    expect(dialog).not.toContainElement(screen.getByRole("status"));
    act(() => vi.advanceTimersByTime(1200));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("重复提示刷新计时，弹窗内到期后移除提示", () => {
    render(<DialogHost />);
    act(() =>
      useWealthStore.getState().openDialog({
        title: "发布工作规划",
        body: "确认配置",
        buttons: [],
      }),
    );
    act(() => useWealthStore.getState().toast("发布失败"));
    act(() => vi.advanceTimersByTime(2000));
    act(() => useWealthStore.getState().toast("发布失败"));
    act(() => vi.advanceTimersByTime(1200));
    expect(screen.getByRole("status")).toHaveTextContent("发布失败");
    act(() => vi.advanceTimersByTime(2000));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it.each(["both", "showPopover", "hidePopover"])(
    "Popover API 缺少 %s 时，页面和弹窗提示仍可显示并按原计时隐藏",
    (missing) => {
      for (const method of ["showPopover", "hidePopover"] as const) {
        if (missing === "both" || missing === method) {
          Object.defineProperty(HTMLElement.prototype, method, {
            configurable: true,
            writable: true,
            value: undefined,
          });
        }
      }

      const { container } = render(<DialogHost />);
      expect(screen.queryByRole("status")).toBeNull();
      act(() => useWealthStore.getState().toast("页面提示"));
      expect(screen.getByRole("status")).toBeVisible();
      expect(screen.getByRole("status")).not.toHaveAttribute("popover");
      act(() => vi.advanceTimersByTime(3200));
      expect(screen.queryByRole("status")).toBeNull();

      act(() =>
        useWealthStore.getState().openDialog({
          title: "发布工作规划",
          body: "确认配置",
          buttons: [],
        }),
      );
      act(() => useWealthStore.getState().toast("发布失败"));
      const dialog = container.querySelector("dialog")!;
      expect(dialog).toContainElement(screen.getByRole("status"));
      expect(screen.getByRole("status")).toBeVisible();
      act(() => vi.advanceTimersByTime(2000));
      act(() => useWealthStore.getState().toast("发布失败"));
      act(() => vi.advanceTimersByTime(1200));
      act(() => useWealthStore.getState().closeDialog());
      expect(dialog).not.toContainElement(screen.getByRole("status"));
      expect(screen.getByRole("status")).toHaveTextContent("发布失败");
      act(() => vi.advanceTimersByTime(2000));
      expect(screen.queryByRole("status")).toBeNull();
    },
  );
});
