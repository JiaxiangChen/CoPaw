import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ContactStatusFilter } from "./ContactStatusFilter";

afterEach(cleanup);

function renderFilter() {
  const onChange = vi.fn();
  render(
    <table>
      <thead>
        <tr>
          <th>
            <ContactStatusFilter
              value="全部"
              customers={[]}
              onChange={onChange}
            />
          </th>
        </tr>
      </thead>
    </table>,
  );
  return onChange;
}

describe("接触状态表头筛选", () => {
  it("支持方向键、Home/End 和 Escape，关闭后恢复触发器焦点", () => {
    renderFilter();
    const trigger = screen.getByRole("button", { name: "筛选接触状态" });
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog");
    const choices = within(dialog).getAllByRole("button");
    expect(choices[0]).toHaveFocus();
    fireEvent.keyDown(choices[0], { key: "ArrowDown" });
    expect(choices[1]).toHaveFocus();
    fireEvent.keyDown(choices[1], { key: "End" });
    expect(choices[3]).toHaveFocus();
    fireEvent.keyDown(choices[3], { key: "Home" });
    expect(choices[0]).toHaveFocus();
    fireEvent.keyDown(choices[0], { key: "ArrowUp" });
    expect(choices[3]).toHaveFocus();
    fireEvent.keyDown(choices[3], { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it.each(["outside", "scroll", "resize"])("%s 关闭浮层", (event) => {
    renderFilter();
    fireEvent.click(screen.getByRole("button", { name: "筛选接触状态" }));
    if (event === "outside") fireEvent.click(document.body);
    else fireEvent(window, new Event(event));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
