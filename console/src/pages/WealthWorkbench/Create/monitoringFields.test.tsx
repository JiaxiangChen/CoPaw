import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it } from "vitest";
import { MonitoringFields } from "./index";
import { defaultMonitoring } from "../monitoring";
import { useWealthStore } from "../store";

function Harness() {
  const config = useWealthStore((state) => state.draft.items[0].monitoring);
  const update = useWealthStore((state) => state.setItemMonitoring);
  return (
    <MonitoringFields
      sceneName="经营场景"
      config={config ?? defaultMonitoring()}
      onChange={(patch) => update("scene", patch)}
    />
  );
}
const mount = () =>
  render(
    <MemoryRouter>
      <Harness />
    </MemoryRouter>,
  );
beforeEach(() => {
  useWealthStore.setState({
    accountId: "president",
    draft: {
      name: "规划",
      items: [
        {
          id: "scene",
          sceneName: "经营场景",
          categoryLabel: "基金",
          categoryCode: "fund",
          mcpRelations: [],
          direction: "方向",
          cycle: "本月",
          schedule: { type: "daily" },
        },
      ],
    },
  });
});
afterEach(cleanup);

it("完成率清空后可重新输入，不补前导零，留空失焦时显示0", () => {
  const view = mount();
  const input = screen.getByLabelText("经营场景预期完成率");
  fireEvent.change(input, { target: { value: "" } });
  expect(input).toHaveValue(null);
  fireEvent.change(input, { target: { value: "7" } });
  fireEvent.change(input, { target: { value: "70" } });
  expect((input as HTMLInputElement).value).toBe("70");
  expect(
    useWealthStore.getState().draft.items[0].monitoring!.expectedCompletionRate,
  ).toBe(70);
  view.unmount();
  mount();
  const restored = screen.getByLabelText("经营场景预期完成率");
  expect(restored).toHaveValue(70);
  fireEvent.change(restored, { target: { value: "" } });
  fireEvent.blur(restored);
  expect(restored).toHaveValue(0);
  expect(
    useWealthStore.getState().draft.items[0].monitoring!.expectedCompletionRate,
  ).toBe(0);
});

it("默认60%和每日09:00，编辑后重新挂载可正常回显", () => {
  const view = mount();
  expect(screen.getByLabelText("经营场景预期完成率")).toHaveValue(60);
  expect(screen.getByText("督办推送时间")).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "有效期内" }),
  ).not.toBeInTheDocument();
  expect(
    within(screen.getByRole("group", { name: "经营场景监控周期" })).getByRole(
      "button",
      { name: "当日" },
    ),
  ).toHaveAttribute("aria-pressed", "true");
  expect(
    screen.getByRole("button", { name: "经营场景督办推送时间" }),
  ).toHaveTextContent("09:00");
  fireEvent.change(screen.getByLabelText("经营场景预期完成率"), {
    target: { value: "85" },
  });
  fireEvent.click(
    within(screen.getByRole("group", { name: "经营场景监控周期" })).getByRole(
      "button",
      { name: "T+7日内" },
    ),
  );
  fireEvent.click(
    within(screen.getByRole("group", { name: "经营场景监控频率" })).getByRole(
      "button",
      { name: "每周" },
    ),
  );
  const days = screen.getByRole("group", { name: "经营场景监控日" });
  fireEvent.click(within(days).getByRole("button", { name: "星期二" }));
  fireEvent.click(within(days).getByRole("button", { name: "星期一" }));
  fireEvent.click(screen.getByRole("button", { name: "经营场景督办推送时间" }));
  fireEvent.click(
    within(screen.getByLabelText("小时")).getByRole("button", {
      name: "12",
    }),
  );
  fireEvent.click(
    within(screen.getByLabelText("分钟")).getByRole("button", {
      name: "30",
    }),
  );
  view.unmount();
  mount();
  expect(screen.getByLabelText("经营场景预期完成率")).toHaveValue(85);
  expect(
    within(screen.getByRole("group", { name: "经营场景监控周期" })).getByRole(
      "button",
      { name: "T+7日内" },
    ),
  ).toHaveAttribute("aria-pressed", "true");
  expect(
    screen.getByRole("button", { name: "经营场景督办推送时间" }),
  ).toHaveTextContent("12:30");
  expect(screen.getByRole("button", { name: "星期二" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(
    useWealthStore.getState().draft.items[0].monitoring!.schedule.daysOfWeek,
  ).toEqual(["tue"]);
});

it("完成率限制在0至100之间，每周至少保留一个监控日", () => {
  mount();
  fireEvent.change(screen.getByLabelText("经营场景预期完成率"), {
    target: { value: "101" },
  });
  expect(screen.getByLabelText("经营场景预期完成率")).toHaveValue(100);
  fireEvent.change(screen.getByLabelText("经营场景预期完成率"), {
    target: { value: "-1" },
  });
  expect(screen.getByLabelText("经营场景预期完成率")).toHaveValue(0);
  fireEvent.click(
    within(screen.getByRole("group", { name: "经营场景监控频率" })).getByRole(
      "button",
      { name: "每周" },
    ),
  );
  expect(screen.getByRole("button", { name: "星期一" })).toBeDisabled();
});
