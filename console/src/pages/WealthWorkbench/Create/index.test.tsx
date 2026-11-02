import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useIframeStore } from "../../../stores/iframeStore";
import styles from "../index.module.less";
import { useWealthStore } from "../store";
import type { Plan, PlanItem, Scene } from "../types";
import { cycleRange } from "../utils";
import Create, {
  SceneDescription,
  ScheduleEditor,
  StepIndicator,
  TaskSchedule,
} from "./index";

vi.mock("../../../api/request", () => ({
  request: vi.fn().mockResolvedValue({ items: [] }),
}));

afterEach(cleanup);

describe("SceneDescription", () => {
  it("用两行截断样式展示描述，并保留完整文本供悬停查看", () => {
    const description =
      "这是一段超过两行时仍可通过悬停查看全文的经营场景技能描述";

    render(<SceneDescription description={description} />);

    const element = screen.getByText(description);
    expect(element).toHaveClass(styles.sceneDescription);
    expect(element).toHaveAttribute("title", description);
  });
});

describe("StepIndicator", () => {
  it("区分当前、已完成和未开始步骤的图标状态", () => {
    const { rerender } = render(
      <StepIndicator active completed={false} number={1} />,
    );
    expect(screen.getByText("1")).toHaveClass(styles.stepActive);
    expect(screen.getByText("1")).not.toHaveClass(styles.stepComplete);

    rerender(<StepIndicator active={false} completed number={1} />);
    expect(screen.getByText("✓")).toHaveClass(styles.stepComplete);
    expect(screen.getByText("✓")).not.toHaveClass(styles.stepActive);

    rerender(<StepIndicator active={false} completed={false} number={2} />);
    expect(screen.getByText("2")).not.toHaveClass(styles.stepActive);
    expect(screen.getByText("2")).not.toHaveClass(styles.stepComplete);
  });
});

describe("ScheduleEditor", () => {
  it.each([
    ["daily", "0 9 * * *"],
    ["weekly", "0 9 * * mon"],
    ["monthly", "0 9 15 * *"],
    ["yearly", "0 9 15 6 *"],
  ])("自定义 %s 展示规则与时刻，不重复展示摘要", (mode, rawCron) => {
    const item: PlanItem = {
      id: "scene-compact",
      sceneName: "紧凑排程场景",
      categoryLabel: "理财",
      categoryCode: "finance",
      mcpRelations: [],
      direction: "优先经营",
      cycle: "本月",
      schedule: { type: "custom", rawCron },
    };
    const { container, unmount } = render(
      <ScheduleEditor item={item} sceneName={item.sceneName} />,
    );
    const repeatRule = screen.getByLabelText("紧凑排程场景自定义重复规则");
    const time = screen.getByRole("button", {
      name: "紧凑排程场景自定义名单推送时间",
    });
    expect(repeatRule).toHaveValue(mode);
    expect(repeatRule.closest(`.${styles.customScheduleRow}`)).toContainElement(
      time,
    );
    expect(container.querySelector(`.${styles.taskRangeLabel}`)).toBeNull();
    if (mode === "weekly")
      expect(screen.getByRole("button", { name: "一" })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
    unmount();
  });

  it("执行频率按钮提供全部频率，自定义区展示可读规则", () => {
    const item: PlanItem = {
      id: "scene-1",
      sceneName: "保障潜客经营",
      categoryLabel: "保险",
      categoryCode: "insurance",
      mcpRelations: [],
      direction: "优先触达高潜客户",
      cycle: "本月",
      start: "2026-09-01",
      end: "2026-09-30",
      schedule: { type: "custom", rawCron: "*/15 * * * *" },
    };

    render(<ScheduleEditor item={item} sceneName={item.sceneName} />);

    const frequency = screen.getByRole("group", {
      name: "保障潜客经营执行频率",
    });
    expect(
      within(frequency)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["每小时", "每日", "每周", "自定义"]);
    expect(
      within(frequency).getByRole("button", { name: "自定义" }),
    ).toHaveAttribute("aria-pressed", "true");

    const repeatRule = screen.getByLabelText("保障潜客经营自定义重复规则");
    expect(repeatRule).toHaveValue("frequency");
    expect(repeatRule).toHaveTextContent("每日");
    expect(repeatRule).toHaveTextContent("每周");
    expect(repeatRule).toHaveTextContent("每月");
    expect(repeatRule).toHaveTextContent("每年");
    expect(repeatRule).toHaveTextContent("自定义频率");
    expect(screen.getByLabelText("保障潜客经营自定义频率单位")).toHaveValue(
      "minutes",
    );
    expect(screen.getByLabelText("保障潜客经营自定义频率间隔")).toHaveValue(15);
    expect(screen.queryByText("每隔 15 分钟")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/cron/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/cron/i)).not.toBeInTheDocument();
  });
});

describe("TaskSchedule", () => {
  it.each(["本月", "本季", "今日", "T+1日", "自定义"])(
    "点击当前周期 %s 会重新应用日期，自定义保留原值，初始渲染不更新",
    (cycle) => {
      const item: PlanItem = {
        id: "repeat-cycle",
        sceneName: "重选周期场景",
        categoryLabel: "理财",
        categoryCode: "finance",
        mcpRelations: [],
        direction: "保留经营方向",
        custNum: 500,
        cycle,
        start: "2000-01-02",
        end: "2000-01-03",
        schedule: { type: "daily", hour: 14, minute: 35 },
      };
      const scene: Scene = {
        id: item.id,
        name: item.sceneName,
        category: "理财",
        categoryCode: "finance",
        icon: "bank",
        desc: "",
        source: "总部预置",
        mcpRelations: [],
      };
      const untouched = { ...item, id: "untouched" };
      useWealthStore.setState({
        draft: { name: "周期重选", items: [item, untouched] },
      });
      function LiveTaskSchedule() {
        const current = useWealthStore((state) => state.draft.items[0]);
        return <TaskSchedule item={current} scene={scene} />;
      }
      const { unmount } = render(<LiveTaskSchedule />);
      const option = within(
        screen.getByRole("group", { name: "重选周期场景任务周期" }),
      ).getByRole("button", { name: cycle });
      expect(option).toHaveAttribute("aria-pressed", "true");
      expect(useWealthStore.getState().draft.items[0]).toEqual(item);
      fireEvent.click(option);
      const range = cycleRange(cycle) ?? [item.start, item.end];
      expect(useWealthStore.getState().draft.items[0]).toEqual({
        ...item,
        start: range[0],
        end: range[1],
      });
      expect(useWealthStore.getState().draft.items[1]).toEqual(untouched);
      unmount();
    },
  );

  it("点击自定义周期的日期数字区域会打开统一样式的日期面板", () => {
    const item: PlanItem = {
      id: "scene-1",
      sceneName: "保障潜客经营",
      categoryLabel: "保险",
      categoryCode: "insurance",
      mcpRelations: [],
      direction: "优先触达高潜客户",
      cycle: "自定义",
      start: "2026-09-01",
      end: "2026-09-30",
      schedule: { type: "daily", hour: 9, minute: 0 },
    };
    const scene: Scene = {
      id: "scene-1",
      name: "保障潜客经营",
      category: "保险",
      categoryCode: "insurance",
      icon: "shield",
      desc: "挖掘高潜保险客户",
      source: "总部预置",
      mcpRelations: [],
    };

    const { container } = render(<TaskSchedule item={item} scene={scene} />);
    const startDate = screen.getByLabelText("保障潜客经营任务开始日期");

    expect(container.querySelector('input[type="date"]')).toBeNull();
    fireEvent.click(startDate);
    expect(document.querySelector(".ant-picker-dropdown")).toBeInTheDocument();
  });
});

describe("Create", () => {
  it("推送客户数支持输入、清除、非法值提示并在发布确认展示", () => {
    const item: PlanItem = {
      id: "scene-limit",
      sceneName: "数量设置场景",
      categoryLabel: "理财",
      categoryCode: "finance",
      mcpRelations: [],
      direction: "优先经营",
      cycle: "本月",
      start: "2026-09-01",
      end: "2026-09-30",
      schedule: { type: "daily", hour: 9, minute: 0 },
    };
    useIframeStore.setState({ positionId: "RB0101", userId: "rm-1" });
    useWealthStore.setState({
      accountId: "rm",
      plansLoaded: true,
      plans: [],
      draft: {
        name: "客户数设置规划",
        items: [item, { ...item, id: "unlimited", sceneName: "不限场景" }],
      },
      scenesByCategory: { "": [] },
      scenesLoading: false,
      editingId: null,
      dialog: null,
      acting: false,
    });
    render(
      <MemoryRouter>
        <Create />
      </MemoryRouter>,
    );
    const header = screen.getByText("经营场景 / 经营方向").parentElement!;
    expect(Array.from(header.children).map((node) => node.textContent)).toEqual(
      ["经营场景 / 经营方向", "执行周期", "推送客户数（选填）", "操作"],
    );
    expect(
      screen.queryByText("经营方向", { exact: true }),
    ).not.toBeInTheDocument();
    const row = header.nextElementSibling!;
    expect(row.children).toHaveLength(4);
    expect(
      Array.from(row.children[1].children).map((node) =>
        node.getAttribute("data-label"),
      ),
    ).toEqual(["任务周期", "执行频率"]);
    expect(
      screen.queryByRole("button", { name: /^上移|^下移/ }),
    ).not.toBeInTheDocument();
    expect(document.querySelector('[draggable="true"]')).toBeNull();
    expect(
      screen
        .getByLabelText("数量设置场景的经营方向")
        .closest(`.${styles.selectedMain}`),
    ).toHaveTextContent("数量设置场景");
    expect(screen.getByText("推送客户数（选填）")).toBeInTheDocument();
    expect(screen.queryByText("每日 09:00")).not.toBeInTheDocument();
    const input = screen.getByRole("spinbutton", {
      name: "数量设置场景的推送客户数",
    });
    expect(input).toHaveValue(null);
    expect(input).toHaveAttribute("placeholder", "不限");
    fireEvent.click(
      within(
        screen.getByRole("group", { name: "数量设置场景任务周期" }),
      ).getByRole("button", { name: "今日" }),
    );
    expect(useWealthStore.getState().draft.items[0].cycle).toBe("今日");
    fireEvent.click(
      within(
        screen.getByRole("group", { name: "数量设置场景执行频率" }),
      ).getByRole("button", { name: "自定义" }),
    );
    expect(useWealthStore.getState().draft.items[0].schedule).toMatchObject({
      type: "custom",
      rawCron: "*/30 * * * *",
    });
    fireEvent.change(screen.getByLabelText("数量设置场景自定义重复规则"), {
      target: { value: "weekly" },
    });
    expect(useWealthStore.getState().draft.items[0].schedule.rawCron).toBe(
      "0 9 * * mon",
    );
    fireEvent.change(input, { target: { value: "500" } });
    expect(useWealthStore.getState().draft.items[0].custNum).toBe(500);
    fireEvent.change(input, { target: { value: "" } });
    expect(useWealthStore.getState().draft.items[0].custNum).toBeUndefined();
    fireEvent.change(input, { target: { value: "0" } });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("请输入正整数")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "发布规划" })[0]);
    expect(useWealthStore.getState().dialog).toBeNull();
    expect(useWealthStore.getState().toastText).toContain("正整数的推送客户数");
    fireEvent.change(input, { target: { value: "500" } });
    fireEvent.click(screen.getAllByRole("button", { name: "发布规划" })[0]);
    const { container } = render(<>{useWealthStore.getState().dialog?.body}</>);
    expect(container).toHaveTextContent("推送客户数：500 人");
    expect(container).toHaveTextContent("推送客户数：不限");
  });

  it("客户经理选择已被规划占用的场景时列出冲突并禁止发布", () => {
    const item: PlanItem = {
      id: "scene-1",
      sceneName: "保障潜客经营",
      categoryLabel: "保险",
      categoryCode: "insurance",
      mcpRelations: [],
      direction: "优先触达高潜客户",
      cycle: "本月",
      start: "2026-09-01",
      end: "2026-09-30",
      schedule: { type: "daily", hour: 9, minute: 0 },
    };
    const occupiedPlan: Plan = {
      id: "plan-president",
      name: "行长重点经营规划",
      source: "行长关注",
      desc: "",
      customers: 0,
      tasks: 0,
      rate: 0,
      status: "已自动下发",
      publishStatus: "published",
      period: "本月",
      start: "2026-09-01",
      end: "2026-09-30",
      items: [item],
      editable: false,
      targetSapIds: ["rm-1"],
    };
    const otherRmPlan: Plan = {
      ...occupiedPlan,
      id: "plan-other-rm",
      name: "其他客户经理规划",
      source: "我的关注",
      editable: false,
    };
    useIframeStore.setState({ positionId: "RB0101", userId: "rm-1" });
    useWealthStore.setState({
      accountId: "rm",
      plansLoaded: true,
      draft: { name: "客户经理规划", items: [item] },
      plans: [otherRmPlan, occupiedPlan],
      scenesByCategory: { "": [] },
      scenesLoading: false,
      editingId: null,
      dialog: null,
      acting: false,
    });

    render(
      <MemoryRouter>
        <Create />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "发布规划" })[0]);

    const dialog = useWealthStore.getState().dialog;
    expect(dialog?.buttons.find((button) => button.primary)?.disabled).toBe(
      true,
    );
    render(<>{dialog?.body}</>);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveClass(styles.sceneConflictAlert);
    expect(alert).toHaveTextContent("保障潜客经营");
    expect(alert).toHaveTextContent("行长重点经营规划");
    expect(screen.queryByText(/其他客户经理规划/)).not.toBeInTheDocument();
  });

  it("编辑自身已有规划时排除当前规划，修改执行周期后仍可确认", () => {
    const item: PlanItem = {
      id: "scene-1",
      sceneName: "保障潜客经营",
      categoryLabel: "保险",
      categoryCode: "insurance",
      mcpRelations: [],
      direction: "优先触达高潜客户",
      cycle: "本月",
      start: "2026-09-01",
      end: "2026-09-30",
      schedule: { type: "daily", hour: 10, minute: 0 },
    };
    const ownPlan: Plan = {
      id: "plan-self",
      name: "客户经理已有规划",
      source: "我的关注",
      desc: "",
      customers: 0,
      tasks: 0,
      rate: 0,
      status: "已自动下发",
      publishStatus: "published",
      period: "本月",
      start: "2026-09-01",
      end: "2026-09-30",
      items: [item],
      editable: true,
      targetSapIds: ["rm-1"],
    };
    const managementPlan: Plan = {
      ...ownPlan,
      id: "plan-president",
      name: "行长历史规划",
      source: "行长关注",
      editable: false,
    };
    useIframeStore.setState({ positionId: "RB0101", userId: "rm-1" });
    useWealthStore.setState({
      accountId: "rm",
      plansLoaded: true,
      draft: { name: ownPlan.name, items: [item] },
      plans: [ownPlan, managementPlan],
      scenesByCategory: { "": [] },
      scenesLoading: false,
      editingId: ownPlan.id,
      dialog: null,
      acting: false,
    });

    render(
      <MemoryRouter>
        <Create />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "保存修改" })[0]);

    const dialog = useWealthStore.getState().dialog;
    expect(dialog?.buttons.find((button) => button.primary)?.disabled).toBe(
      false,
    );
    const confirmation = render(<>{dialog?.body}</>);
    expect(within(confirmation.container).queryByRole("alert")).toBeNull();
  });

  it("规划数据未就绪时禁用发布，避免跳过已有规划冲突检查", () => {
    useIframeStore.setState({ positionId: "RB0101", userId: "rm-1" });
    useWealthStore.setState({
      accountId: "rm",
      plansLoaded: false,
      draft: { name: "客户经理规划", items: [] },
      plans: [],
      scenesByCategory: { "": [] },
      scenesLoading: false,
      editingId: null,
      dialog: null,
      acting: false,
    });

    const { container } = render(
      <MemoryRouter>
        <Create />
      </MemoryRouter>,
    );

    const publishButtons = within(container).getAllByRole("button", {
      name: "规划加载中…",
    });
    expect(publishButtons).toHaveLength(2);
    publishButtons.forEach((button) => expect(button).toBeDisabled());
    expect(useWealthStore.getState().dialog).toBeNull();
  });

  it("规划数据未就绪时不允许编辑表单，但仍可取消返回", () => {
    useIframeStore.setState({ positionId: "RB0101", userId: "rm-1" });
    useWealthStore.setState({
      accountId: "rm",
      plansLoaded: false,
      draft: { name: "", items: [] },
      plans: [],
      scenesByCategory: { "": [] },
      scenesLoading: false,
      editingId: null,
      dialog: null,
      acting: false,
    });

    const { container } = render(
      <MemoryRouter>
        <Create />
      </MemoryRouter>,
    );

    expect(
      within(container).getByRole("button", { name: "全部" }),
    ).toBeDisabled();
    expect(screen.getByLabelText("规划名称")).toBeDisabled();
    expect(
      within(container).getByRole("button", { name: "上一步" }),
    ).toBeDisabled();
    expect(
      within(container).getByRole("button", { name: "取消" }),
    ).toBeEnabled();
  });
});
