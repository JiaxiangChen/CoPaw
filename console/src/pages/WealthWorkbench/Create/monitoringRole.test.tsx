import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { useWealthStore } from "../store";
import { useIframeStore } from "../../../stores/iframeStore";
import { cycleRange } from "../utils";
import { defaultMonitoring } from "../monitoring";
import styles from "../index.module.less";
import Create from "./index";
vi.mock("../../../api/request", () => ({
  request: vi.fn().mockResolvedValue({ items: [] }),
}));
afterEach(cleanup);
it.each([
  ["rm", "RB0101"],
  ["middle", "RB0301"],
  ["president", "RB1101"],
] as const)("%s 创建页按角色展示督办列", (accountId, positionId) => {
  const [start, end] = cycleRange("本月")!;
  useIframeStore.setState({ positionId, userId: "u" });
  useWealthStore.setState({
    accountId,
    plansLoaded: true,
    plans: [],
    editingId: null,
    draft: {
      name: "规划",
      items: [
        {
          id: "scene",
          sceneName: "经营场景",
          direction: "方向",
          categoryLabel: "基金",
          categoryCode: "fund",
          mcpRelations: [],
          cycle: "本月",
          start,
          end,
          schedule: { type: "daily", hour: 9, minute: 0 },
          monitoring: defaultMonitoring(),
        },
      ],
    },
  });
  const view = render(
    <MemoryRouter>
      <Create />
    </MemoryRouter>,
  );
  const list = view.container.querySelector(`.${styles.selectedList}`)!;
  if (accountId === "president") {
    expect(screen.getByText("督办策略")).toBeInTheDocument();
    expect(screen.getByLabelText("经营场景预期完成率")).toHaveValue(60);
    expect(list).not.toHaveClass(styles.withoutMonitoring);
  } else {
    expect(screen.queryByText("督办策略")).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("经营场景预期完成率"),
    ).not.toBeInTheDocument();
    expect(list).toHaveClass(styles.withoutMonitoring);
  }
  expect(
    screen.queryByRole("button", { name: "有效期内" }),
  ).not.toBeInTheDocument();
});
