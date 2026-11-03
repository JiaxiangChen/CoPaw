import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import TaskTitle from "./TaskTitle";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function mockWidths(scrollWidth: number, clientWidth: number) {
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(
    scrollWidth,
  );
  return vi
    .spyOn(HTMLElement.prototype, "clientWidth", "get")
    .mockReturnValue(clientWidth);
}

describe("TaskTitle", () => {
  it.each([80, 100])(
    "does not show a tooltip when text fits (%s)",
    async (width) => {
      mockWidths(width, 100);
      render(<TaskTitle title="每日巡检" className="task-title" />);
      fireEvent.mouseEnter(screen.getByText("每日巡检"));
      await act(() => new Promise((resolve) => setTimeout(resolve, 200)));
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    },
  );

  it("shows the full overflowing title and preserves clicks", async () => {
    mockWidths(200, 100);
    const onClick = vi.fn();
    render(
      <div onClick={onClick}>
        <TaskTitle title="被省略的完整任务标题" className="task-title" />
      </div>,
    );
    const title = screen.getByText("被省略的完整任务标题");
    expect(title).toHaveClass("task-title");
    fireEvent.mouseEnter(title);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "被省略的完整任务标题",
    );
    fireEvent.click(title);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("remeasures on resize and title changes and disconnects on unmount", async () => {
    let onResize = () => {};
    const disconnect = vi.fn();
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: () => void) {
          onResize = callback;
        }
        observe = vi.fn();
        disconnect = disconnect;
      },
    );
    const width = mockWidths(200, 100);
    const view = render(<TaskTitle title="长标题" className="task-title" />);
    fireEvent.mouseEnter(screen.getByText("长标题"));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("长标题");
    width.mockReturnValue(300);
    act(() => onResize());
    await waitFor(
      () => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument(),
      { timeout: 3000 },
    );
    width.mockReturnValue(100);
    view.rerender(<TaskTitle title="更新后的长标题" className="task-title" />);
    fireEvent.mouseEnter(
      screen.getByText("更新后的长标题", { selector: "span" }),
    );
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "更新后的长标题",
    );
    view.unmount();
    expect(disconnect).toHaveBeenCalled();
  });
});
