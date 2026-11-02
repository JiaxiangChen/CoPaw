import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import Sidebar from "./Sidebar";

const iframeState = vi.hoisted(() => ({
  isSuperManager: false,
  manager: false,
  hideChat: false,
  source: "RMASSIST" as string | null,
  space: null as string | null,
  bbk: null as string | null,
}));

vi.mock("antd", async () => ({
  ...(await vi.importActual<typeof import("antd")>("antd")),
  Tooltip: ({ title, children }: { title: string; children: ReactNode }) => (
    <span data-tooltip-title={title}>{children}</span>
  ),
}));

vi.mock(
  "@agentscope-ai/icons",
  () =>
    new Proxy(
      {},
      {
        get: (_, property) => (property === "then" ? undefined : () => null),
        has: () => true,
      },
    ),
);

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string | { defaultValue?: string }) =>
      typeof fallback === "string" ? fallback : fallback?.defaultValue || key,
  }),
}));

vi.mock("../api/modules/auth", () => ({
  authApi: {
    getStatus: vi.fn().mockResolvedValue({ enabled: false }),
  },
}));

vi.mock("../contexts/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

vi.mock("../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: {} }),
}));

vi.mock("../stores/iframeStore", () => ({
  useIframeStore: (selector: (state: typeof iframeState) => unknown) =>
    selector(iframeState),
}));

afterEach(() => {
  cleanup();
  iframeState.source = "RMASSIST";
  iframeState.space = null;
  iframeState.bbk = null;
  iframeState.isSuperManager = false;
  iframeState.manager = false;
  iframeState.hideChat = false;
});

// Keep the pre-refactor mode differences explicit, including missing entries.
const expandedLabels = [
  "nav.chat",
  "nav.mySkills",
  "nav.myMcp",
  "nav.myExperts",
  "nav.workspace",
  "nav.market",
  "专家社区",
  "nav.cronJobs",
  "定时任务（新）",
  "nav.channels",
  "nav.agentConfig",
  "nav.tools",
  "nav.hookManagement",
  "nav.models",
  "精选案例管理",
  "场景预设管理",
  "nav.environments",
  "nav.security",
  "Skill 配置",
  "系统特性配置",
  "系统自检",
  "运营看板",
  "质量工程看板",
  "批调度监控",
  "批调度看板（新）",
  "Claw数据看板",
  "Claw技能运行看板",
  "Messages",
  "异步任务中心",
  "持续治理",
];
const conditionalLabels = new Set([
  "场景预设管理",
  "Skill 配置",
  "系统特性配置",
  "系统自检",
  "质量工程看板",
  "批调度监控",
  "批调度看板（新）",
  "Claw数据看板",
  "Claw技能运行看板",
  "异步任务中心",
]);
const managementLabels = ["场景预设管理", "系统特性配置", "系统自检"];
const governanceLabels = ["质量工程看板", "批调度监控", "批调度看板（新）"];
const clawLabels = ["Claw数据看板", "Claw技能运行看板"];

describe("Sidebar behavior preservation", () => {
  const scenarios: {
    name: string;
    state: Partial<typeof iframeState>;
    visible: string[];
  }[] = [
    { name: "default", state: {}, visible: ["Skill 配置", "异步任务中心"] },
    {
      name: "other source",
      state: { source: "OTHER" },
      visible: ["异步任务中心"],
    },
    { name: "null source", state: { source: null }, visible: ["异步任务中心"] },
    {
      name: "super manager",
      state: { isSuperManager: true },
      visible: [
        "Skill 配置",
        "异步任务中心",
        ...managementLabels,
        ...governanceLabels,
      ],
    },
    {
      name: "manager",
      state: { manager: true },
      visible: [
        "Skill 配置",
        "异步任务中心",
        ...managementLabels,
        ...governanceLabels,
      ],
    },
    {
      name: "RM space without bbk",
      state: { space: "RM_ASSISTANCE", manager: true },
      visible: ["Skill 配置", ...managementLabels, ...clawLabels],
    },
    {
      name: "RM space other bbk",
      state: { space: "RM_ASSISTANCE", bbk: "200", isSuperManager: true },
      visible: ["Skill 配置", ...managementLabels, ...clawLabels],
    },
    {
      name: "RM space bbk 100 manager",
      state: {
        source: "OTHER",
        space: "RM_ASSISTANCE",
        bbk: "100",
        manager: true,
      },
      visible: [
        "异步任务中心",
        ...managementLabels,
        ...governanceLabels,
        ...clawLabels,
      ],
    },
    {
      name: "RM space bbk 100 regular user",
      state: { space: "RM_ASSISTANCE", bbk: "100" },
      visible: ["Skill 配置", "异步任务中心", ...clawLabels],
    },
  ];

  describe.each([false, true])("hideChat=%s", (hideChat) => {
    it.each(scenarios)(
      "preserves menus and order: $name",
      ({ state, visible }) => {
        Object.assign(iframeState, state, { hideChat });
        const { container } = render(
          <MemoryRouter>
            <Sidebar selectedKey="models" />
          </MemoryRouter>,
        );
        const expected = expandedLabels.filter(
          (label) => !conditionalLabels.has(label) || visible.includes(label),
        );
        const expanded = expected.filter(
          (label) =>
            !hideChat || !["nav.chat", "运营看板", "Messages"].includes(label),
        );
        expect(
          screen
            .getAllByRole("menuitem")
            .filter((item) => !item.hasAttribute("aria-haspopup"))
            .map((item) => item.textContent),
        ).toEqual(expanded);

        // The collapse toggle is the only button when auth is disabled.
        fireEvent.click(screen.getByRole("button"));
        expect(
          Array.from(container.querySelectorAll("[data-tooltip-title]")).map(
            (item) => item.getAttribute("data-tooltip-title"),
          ),
        ).toEqual(
          expected.filter(
            (label) =>
              !["专家社区", "nav.hookManagement"].includes(label) &&
              (!hideChat || label !== "Messages"),
          ),
        );
      },
    );
  });

  it("preserves different Claw skill navigation in each mode", () => {
    iframeState.space = "RM_ASSISTANCE";
    const { container } = render(
      <MemoryRouter initialEntries={["/models"]}>
        <Sidebar selectedKey="models" />
        <LocationProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("Claw技能运行看板"));
    expect(screen.getByTestId("location")).toHaveTextContent("/models");

    fireEvent.click(screen.getByRole("button"));
    const skillLink = container.querySelector(
      '[data-tooltip-title="Claw技能运行看板"]',
    )!;
    fireEvent.click(within(skillLink as HTMLElement).getByRole("button"));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/analytics/claw-skill-data-overview",
    );
  });
});

describe("Sidebar skill config visibility", () => {
  it("shows the skill config menu for the RMASSIST source", () => {
    render(
      <MemoryRouter>
        <Sidebar selectedKey="skill-config" />
      </MemoryRouter>,
    );

    expect(screen.getByText("Skill 配置")).toBeInTheDocument();
  });

  it("hides the skill config menu for other sources", () => {
    iframeState.source = "OTHER";

    render(
      <MemoryRouter>
        <Sidebar selectedKey="models" />
      </MemoryRouter>,
    );

    expect(screen.queryByText("Skill 配置")).not.toBeInTheDocument();
  });
});

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

describe("Sidebar Claw report navigation", () => {
  it("places the Claw dashboard after operations and navigates to its route", () => {
    iframeState.source = "OTHER";
    iframeState.space = "RM_ASSISTANCE";
    render(
      <MemoryRouter>
        <Sidebar selectedKey="analytics-business-overview" />
        <LocationProbe />
      </MemoryRouter>,
    );

    const operations = screen.getByText("运营看板");
    const claw = screen.getByText("Claw数据看板");
    expect(
      operations.compareDocumentPosition(claw) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    fireEvent.click(claw);
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/analytics/claw-data-overview",
    );
    expect(screen.getAllByText("Claw数据看板")).toHaveLength(1);
    expect(screen.getByText("Claw技能运行看板")).toBeInTheDocument();
  });

  it("hides the Claw dashboards outside the RM_ASSISTANCE space", () => {
    render(
      <MemoryRouter>
        <Sidebar selectedKey="analytics-business-overview" />
      </MemoryRouter>,
    );

    expect(screen.queryByText("Claw数据看板")).not.toBeInTheDocument();
    expect(screen.queryByText("Claw技能运行看板")).not.toBeInTheDocument();
  });
});
