import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { useWealthStore } from "../store";
import { todayKey } from "../utils";
import Board from "./index";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  fetchAvailableSceneCount: vi.fn().mockResolvedValue(0),
  fetchSkillStats: vi.fn().mockResolvedValue({}),
}));
function Location() {
  const location = useLocation();
  return (
    <output>
      {location.pathname}
      {location.search}
    </output>
  );
}
afterEach(cleanup);
it("看板内部编辑回填计划后跳转创建页，不依赖编辑链接", async () => {
  const id = "plan/a & b";
  useWealthStore.setState({
    accountId: "rm",
    plansLoaded: true,
    plans: [
      {
        id,
        name: "跳转测试规划",
        source: "我的关注",
        desc: "",
        customers: 0,
        tasks: 0,
        rate: 0,
        status: "发布成功",
        period: "今日",
        start: todayKey(),
        end: todayKey(),
        items: [],
        editable: true,
      },
    ],
  });
  render(
    <MemoryRouter initialEntries={["/wealth/board?origin=Y&hideLogo=true"]}>
      <Location />
      <Board />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "列表模式" }));
  fireEvent.click(screen.getByRole("button", { name: "修改规划" }));
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("/wealth/create"),
  );
  expect(screen.getByRole("status")).not.toHaveTextContent("planId=");
  expect(useWealthStore.getState().editingId).toBe(id);
  expect(useWealthStore.getState().draft.name).toBe("跳转测试规划");
});
