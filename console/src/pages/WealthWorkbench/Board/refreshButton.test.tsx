import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { useWealthStore } from "../store";
import { fetchSkillStats } from "../api";
import type { SkillStat } from "../types";
import { todayKey } from "../utils";
import Board from "./index";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  fetchAvailableSceneCount: vi.fn().mockResolvedValue(0),
  fetchSkillStats: vi.fn(),
}));
const originalRefresh = useWealthStore.getState().refreshPlans;
afterEach(() => {
  cleanup();
  useWealthStore.setState({ refreshPlans: originalRefresh });
});

it("刷新入口仅显示图标，加载和手动更新通过旋转/禁用反馈而无加载文字", async () => {
  let initial!: (data: Record<string, SkillStat>) => void;
  let updated!: (data: Record<string, SkillStat>) => void;
  vi.mocked(fetchSkillStats)
    .mockReturnValueOnce(
      new Promise((resolve) => {
        initial = resolve;
      }),
    )
    .mockReturnValueOnce(
      new Promise((resolve) => {
        updated = resolve;
      }),
    );
  const refresh = vi.fn().mockResolvedValue(undefined);
  const day = todayKey();
  useWealthStore.setState({
    accountId: "rm",
    plansLoaded: true,
    refreshPlans: refresh,
    plans: [
      {
        id: "a",
        name: "规划",
        source: "我的关注",
        desc: "",
        period: "今日",
        start: day,
        end: day,
        status: "已自动下发",
        customers: 0,
        tasks: 0,
        rate: 0,
        items: [
          {
            id: "a",
            sceneName: "技能",
            categoryLabel: "理财",
            categoryCode: "finance",
            direction: "客户经营",
            cycle: "今日",
            start: day,
            end: day,
            mcpRelations: [],
            schedule: { type: "daily", hour: 9, minute: 0 },
          },
        ],
      },
    ],
  });
  render(
    <MemoryRouter>
      <Board />
    </MemoryRouter>,
  );
  const button = screen.getByRole("button", { name: "刷新看板" });
  expect(button).toHaveTextContent(/^$/);
  expect(button.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  expect(button).toBeDisabled();
  expect(button).toHaveAttribute("aria-busy", "true");
  expect(screen.queryByText("正在加载客户与任务统计…")).not.toBeInTheDocument();
  await act(async () => initial({}));
  await waitFor(() => expect(button).toBeEnabled());
  await act(async () => fireEvent.click(button));
  expect(refresh).toHaveBeenCalledTimes(1);
  expect(button).toBeDisabled();
  expect(button).toHaveAttribute("aria-busy", "true");
  expect(button.querySelector("svg")?.getAttribute("class")).toContain(
    "taskRefreshBusy",
  );
  expect(screen.queryByText("客户与任务统计更新中…")).not.toBeInTheDocument();
  await act(async () => updated({}));
  expect(button).toBeEnabled();
  expect(button).toHaveAttribute("aria-busy", "false");
});
