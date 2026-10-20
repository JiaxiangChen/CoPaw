import React from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChatPage from "./index";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import type { ManagedJob } from "@/api/types/schedulerCron";

type MockSessionApi = {
  preferredChatId: string;
  onSessionCreated: ((sessionId: string) => void) | null;
  onSessionIdResolved: ((tempId: string, realId: string) => void) | null;
  onSessionRemoved: ((removedId: string) => void) | null;
  onSessionSelected:
    | ((sessionId: string | null | undefined, realId: string | null) => void)
    | null;
  getChatIdForSession: (sessionId: string) => string | null;
  getLogicalSessionId: (sessionId: string) => string;
  getRealIdForSession: (sessionId: string) => string | null;
  getSessionList: ReturnType<typeof vi.fn>;
  setLastUserMessage: ReturnType<typeof vi.fn>;
  updateSession: (session: unknown, options?: unknown) => Promise<unknown>;
};

const mocks = vi.hoisted(() => {
  const setSessionLoading = vi.fn();
  const setSessions = vi.fn();
  const getLoading = vi.fn(() => false);
  const setLoading = vi.fn();

  return {
    capturedOptions: null as Record<string, any> | null,
    taskInfo: vi.fn(),
    planModeEnabledHistory: [] as boolean[],
    showContentOnly: false,
    isOriginY: false,
    createChat: vi.fn(async () => ({
      id: "chat-real-created",
      meta: { plan_mode_enabled: true },
    })),
    expertSelectorProps: null as Record<string, unknown> | null,
    listCronJobs: vi.fn(async () => []),
    listExperts: vi.fn(async () => []),
    loadActiveModelData: vi.fn(async () => ({
      active_llm: {
        provider_id: "provider-1",
        model: "model-1",
      },
    })),
    currentSessionId: "chat-1",
    inputDisabled: true,
    inputLoading: false,
    pathname: "/chat/chat-1",
    search: "",
    getChatIdForSession: vi.fn((sessionId: string) => sessionId),
    getLogicalSessionId: vi.fn((sessionId: string) => sessionId),
    getRealIdForSession: vi.fn((sessionId: string) => sessionId),
    getContextUsage: vi.fn(async () => ({ available: false })),
    navigationSessionId: null as string | null,
    navigationTaskId: null as string | null,
    navigate: vi.fn(),
    sessionApi: null as MockSessionApi | null,
    sessions: [
      {
        id: "chat-1",
        realId: "chat-1",
        sessionId: "chat-1",
        name: "会话 1",
        messages: [],
        meta: { plan_mode_enabled: true },
      },
    ],
    setLoading,
    getLoading,
    setSessionLoading,
    setSessions,
    updateChat: vi.fn(
      async (_chatId: string, payload: Record<string, any>) => ({
        meta: payload.meta,
      }),
    ),
    updateSession: vi.fn(async () => undefined),
    clearNavigationParams: vi.fn(),
  };
});

vi.mock("@/components/agentscope-chat", () => {
  const React = require("react");
  const findPlanModeEnabled = (node: React.ReactNode): boolean | null => {
    for (const item of React.Children.toArray(node)) {
      if (!React.isValidElement(item)) {
        continue;
      }
      if (item.props && "enabled" in item.props) {
        return Boolean(item.props.enabled);
      }
      const childEnabled = findPlanModeEnabled(item.props?.children);
      if (childEnabled !== null) {
        return childEnabled;
      }
    }
    return null;
  };

  return {
    AgentScopeRuntimeWebUIComposedProvider: ({
      options,
      children,
    }: {
      options: Record<string, any>;
      children: React.ReactNode;
    }) => {
      mocks.capturedOptions = options;
      const planModeEnabled = findPlanModeEnabled(
        options.sender?.quickMenuItems,
      );
      if (planModeEnabled !== null) {
        mocks.planModeEnabledHistory.push(planModeEnabled);
      }
      return <>{children}</>;
    },
    AgentScopeRuntimeWebUILayout: React.forwardRef(() => (
      <div>
        <div data-testid="chat-welcome">
          {mocks.capturedOptions?.welcome?.render?.({
            greeting: "你好",
            onSubmit: vi.fn(),
          })}
        </div>
        {!mocks.showContentOnly && (
          <>
            <div data-testid="chat-sender-before-ui">
              {mocks.capturedOptions?.sender?.beforeUI}
            </div>
            <div data-testid="chat-rendered-composer">
              {mocks.capturedOptions?.sender?.renderComposer?.(
                <div data-testid="default-composer">composer</div>,
              )}
            </div>
          </>
        )}
        <div data-testid="chat-sender-prefix">
          {mocks.capturedOptions?.sender?.prefix}
        </div>
      </div>
    )),
    AgentScopeRuntimeRequestCard: () => null,
    AgentScopeRuntimeResponseCard: () => null,
    Attachments: ({ items }: { items: Array<{ name?: string }> }) => (
      <div>
        {items.map((item) => (
          <span key={item.name}>{item.name}</span>
        ))}
      </div>
    ),
    useChatAnywhereSessionsState: () => ({
      sessions: mocks.sessions,
      setSessionLoading: mocks.setSessionLoading,
      setSessions: mocks.setSessions,
      currentSessionId: mocks.currentSessionId,
    }),
    useChatAnywhereInput: (
      selector: (value: {
        disabled: boolean;
        loading: boolean;
        setLoading: typeof mocks.setLoading;
        getLoading: typeof mocks.getLoading;
      }) => unknown,
    ) =>
      selector({
        disabled: mocks.inputDisabled,
        loading: mocks.inputLoading,
        setLoading: mocks.setLoading,
        getLoading: mocks.getLoading,
      }),
  };
});

vi.mock("@/components/agentscope-chat/ComposerQuickMenu", () => {
  function ComposerQuickMenu(props: {
    children?: React.ReactNode;
    disabled?: boolean;
    triggerLabel: string;
  }) {
    return (
      <div>
        <button
          type="button"
          aria-label={props.triggerLabel}
          disabled={props.disabled}
        >
          menu
        </button>
        <div>{props.children}</div>
      </div>
    );
  }

  function ComposerQuickMenuItem(props: {
    icon?: React.ReactNode;
    label: React.ReactNode;
    extra?: React.ReactNode;
    enabled?: boolean;
  }) {
    return (
      <div>
        {props.icon}
        <span>{props.label}</span>
        {props.extra}
      </div>
    );
  }

  function ComposerQuickMenuSubmenu(props: {
    icon?: React.ReactNode;
    label: React.ReactNode;
    children?: React.ReactNode;
    disabled?: boolean;
  }) {
    return (
      <div aria-disabled={props.disabled}>
        {props.icon}
        <span>{props.label}</span>
        <div>{props.children}</div>
      </div>
    );
  }

  return {
    __esModule: true,
    default: ComposerQuickMenu,
    ComposerQuickMenuItem,
    ComposerQuickMenuSubmenu,
  };
});

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock("@agentscope-ai/icons", () => ({
  SparkAttachmentLine: () => <span data-testid="attachment-icon" />,
  SparkCopyLine: () => <span data-testid="copy-icon" />,
  SparkDownLine: () => <span data-testid="expert-selector-arrow" />,
}));

vi.mock("react-router-dom", () => ({
  useLocation: () => ({ pathname: mocks.pathname, search: mocks.search }),
  useNavigate: () => mocks.navigate,
}));

vi.mock("../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: {
      error: vi.fn(),
      success: vi.fn(),
      warning: vi.fn(),
      info: mocks.taskInfo,
    },
  }),
}));

vi.mock("../../contexts/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

vi.mock("../../contexts/BrandThemeContext", () => ({
  useBrandTheme: () => ({
    theme: {
      brandName: "Swe",
      avatar: "",
    },
  }),
}));

vi.mock("../../stores/agentStore", () => {
  const state = { selectedAgent: "default" };
  const useAgentStore = (selector?: (value: typeof state) => unknown) =>
    selector ? selector(state) : state;
  useAgentStore.getState = () => state;
  return { useAgentStore };
});

vi.mock("../../stores/chatPresentationStore", () => ({
  useChatPresentationStore: (
    selector: (value: { showContentOnly: boolean }) => unknown,
  ) => selector({ showContentOnly: mocks.showContentOnly }),
}));

vi.mock("../../stores/sourceSystemConfigStore", () => ({
  useSourceSystemConfigStore: (
    selector: (value: { config: Record<string, unknown> }) => unknown,
  ) => selector({ config: {} }),
}));

vi.mock("../../stores/providerModelStore", () => ({
  useProviderModelStore: (
    selector: (value: {
      loadActiveModelData: typeof mocks.loadActiveModelData;
    }) => unknown,
  ) => selector({ loadActiveModelData: mocks.loadActiveModelData }),
}));

vi.mock("../../stores/iframeStore", () => {
  const useIframeStore = (
    selector?: (value: { userId: string; isOriginY: boolean }) => unknown,
  ) =>
    selector
      ? selector({ userId: "test-user", isOriginY: mocks.isOriginY })
      : { userId: "test-user", isOriginY: mocks.isOriginY };

  useIframeStore.getState = () => ({
    sessionId: mocks.navigationSessionId,
    taskId: mocks.navigationTaskId,
    clearNavigationParams: mocks.clearNavigationParams,
  });

  return {
    getIframeContext: () => ({ userId: "test-user" }),
    useIframeStore,
  };
});

vi.mock("../../api/modules/chat", () => ({
  chatApi: {
    createChat: mocks.createChat,
    getSubAgentRuns: vi.fn(async () => ({
      chat_id: "chat-1",
      session_id: "chat-1",
      runs: [],
    })),
    cancelSubAgentRun: vi.fn(async () => undefined),
    filePreviewUrl: vi.fn((filename: string) => `/preview/${filename}`),
    getRecentGoal: vi.fn(async () => null),
    getContextUsage: mocks.getContextUsage,
    stopChat: vi.fn(async () => undefined),
    updateChat: mocks.updateChat,
    uploadFile: vi.fn(),
  },
  sessionApi: {},
}));

vi.mock("../../api/modules/cronjob", () => ({
  cronJobApi: {
    listCronJobs: mocks.listCronJobs,
    pauseCronJob: vi.fn(async () => undefined),
    resumeCronJob: vi.fn(async () => undefined),
    runCronJob: vi.fn(async () => undefined),
    deleteCronJob: vi.fn(async () => undefined),
    markTaskRead: vi.fn(async () => undefined),
  },
}));

vi.mock("../../api/modules/feedback", () => ({
  feedbackApi: {
    getSessionFeedbacks: vi.fn(async () => ({ items: [] })),
  },
}));

vi.mock("../../api/modules/experts", () => ({
  expertsApi: {
    listExperts: mocks.listExperts,
  },
}));

vi.mock("../../api/modules/provider", () => ({
  providerApi: {
    listProviders: vi.fn(async () => []),
    getActiveModels: vi.fn(async () => []),
  },
}));

vi.mock("../../api/config", () => ({
  getApiUrl: (path: string) => path,
}));

vi.mock("../../api/authHeaders", () => ({
  buildAuthHeaders: () => ({}),
}));

vi.mock("./OptionsPanel/defaultConfig", () => ({
  __esModule: true,
  default: {
    theme: {
      leftHeader: {},
    },
    api: {},
  },
  getDefaultConfig: () => ({
    sender: {},
    welcome: {},
    api: {},
    cards: {},
  }),
}));

vi.mock("./sessionApi", () => {
  const sessionApiMock = {
    preferredChatId: "",
    onSessionCreated: null,
    onSessionIdResolved: null,
    onSessionRemoved: null,
    onSessionSelected: null,
    getChatIdForSession: (sessionId: string) =>
      mocks.getChatIdForSession(sessionId),
    getLogicalSessionId: (sessionId: string) =>
      mocks.getLogicalSessionId(sessionId),
    getRealIdForSession: (sessionId: string) =>
      mocks.getRealIdForSession(sessionId),
    getSessionList: vi.fn(async () => []),
    setLastUserMessage: vi.fn(),
    updateSession: mocks.updateSession,
  };
  mocks.sessionApi = sessionApiMock;
  return {
    __esModule: true,
    default: sessionApiMock,
  };
});

vi.mock("./components/ChatSidebar", () => {
  function ChatSidebar(props: {
    tasks: Array<{ id: string; name?: string }>;
    selectedTaskId?: string;
    onTaskClick?: (task: { id: string; name?: string }) => void;
  }) {
    return (
      <div
        data-testid="chat-sidebar"
        data-selected-task-id={props.selectedTaskId || ""}
      >
        {props.tasks.map((task) => (
          <button
            key={task.id}
            type="button"
            onClick={() => props.onTaskClick?.(task)}
          >
            {`Open ${task.name || task.id}`}
          </button>
        ))}
      </div>
    );
  }

  return {
    __esModule: true,
    default: ChatSidebar,
  };
});

vi.mock("@/components/agentscope-chat/AutoPreviewHtmlContext", () => ({
  AutoPreviewHtmlProvider: ({
    children,
    triggerKey,
  }: {
    children: React.ReactNode;
    triggerKey: number;
    onConsumed: () => void;
  }) => (
    <div data-testid="auto-preview-provider" data-trigger-key={triggerKey}>
      {children}
    </div>
  ),
  useAutoPreviewHtml: () => ({
    enabled: false,
    register: () => () => undefined,
  }),
}));

vi.mock("@/components/agentscope-chat/HtmlPreviewTrackingContext", () => ({
  HtmlPreviewTrackingProvider: ({
    children,
    value,
  }: {
    children: React.ReactNode;
    value: { cronTaskId?: string | null; cronTaskName?: string | null };
  }) => (
    <div
      data-testid="html-preview-tracking-provider"
      data-cron-task-id={value.cronTaskId || ""}
      data-cron-task-name={value.cronTaskName || ""}
    >
      {children}
    </div>
  ),
  useHtmlPreviewTracking: () => ({}),
}));

vi.mock("./components/ChatHeaderTitle", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("./components/ChatSessionInitializer", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("./ModelSelector", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("@/components/ConversationQuickNav", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("./ExpertSelector", () => ({
  __esModule: true,
  default: (props: Record<string, unknown>) => {
    mocks.expertSelectorProps = props;
    return (
      <button
        type="button"
        onClick={() => (props.onChange as (id: string) => void)("expert-1")}
      >
        选择专家
      </button>
    );
  },
}));

vi.mock("@/components/agentscope-chat/DragUploadOverlay", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("./components/GeneratedFilesDrawer", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("./components/TaskProgressFloatingCard", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("./components/RuntimeRequestCard", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("./components/RuntimeResponseCard", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("./components/ApprovalActionCard", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("./components/PlanInteractionCards", () => ({
  ActivePlanInteractionComposer: (props: {
    defaultComposer: React.ReactElement;
    onContinueModifying?: (data: Record<string, any>) => void;
    onPlanModeDecision?: (enabled: boolean) => void;
  }) => (
    <div data-testid="active-plan-interaction-composer">
      {props.defaultComposer}
      <button
        type="button"
        onClick={() =>
          props.onContinueModifying?.({
            card_type: "plan_review",
            plan_id: "plan-123",
            title: "Implementation plan",
            summary: "Plan summary",
            steps: [],
            risks: [],
            verification: [],
          })
        }
      >
        Continue modifying
      </button>
      <button type="button" onClick={() => props.onPlanModeDecision?.(false)}>
        Exit Plan Mode
      </button>
    </div>
  ),
  ActivePlanReviewCard: () => {
    throw new Error("ActivePlanReviewCard should not render outside composer");
  },
  ActivePlanClarificationCard: () => {
    throw new Error(
      "ActivePlanClarificationCard should not render outside composer",
    );
  },
  PlanClarificationCard: () => null,
}));

vi.mock("./components/TaskRunGroupCard", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("./components/SubAgentRunMonitor", () => ({
  __esModule: true,
  default: () => <div data-testid="subagent-run-monitor" />,
}));

vi.mock(
  "@/components/agentscope-chat/AgentScopeRuntimeWebUI/customToolRenders/CopyFileToStatic",
  () => ({
    __esModule: true,
    default: () => null,
  }),
);

vi.mock("./components/ResponseFeedbackCard/whitelist", () => ({
  isResponseFeedbackUserAllowed: () => false,
}));

vi.mock("@/components/agentscope-chat/FeaturedCases", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("@/components/agentscope-chat/CaseDetailDrawer", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("@/api/modules/featuredCases", () => ({
  featuredCasesApi: {
    getCaseDetail: vi.fn(),
  },
}));

vi.mock("@/api/modules/scenarioPreset", () => ({
  scenarioPresetApi: {
    getEffectiveCatalog: vi.fn(async () => ({ domains: [] })),
  },
}));

describe("ChatPage plan mode wiring", () => {
  beforeEach(() => {
    mocks.taskInfo.mockClear();
    vi.spyOn(schedulerCronApi, "listAll").mockResolvedValue([]);
    mocks.capturedOptions = null;
    mocks.planModeEnabledHistory = [];
    mocks.showContentOnly = false;
    mocks.isOriginY = false;
    mocks.inputDisabled = true;
    mocks.inputLoading = false;
    mocks.pathname = "/chat/chat-1";
    mocks.search = "";
    mocks.currentSessionId = "chat-1";
    mocks.getChatIdForSession.mockImplementation(
      (sessionId: string) => sessionId,
    );
    mocks.getLogicalSessionId.mockImplementation(
      (sessionId: string) => sessionId,
    );
    mocks.getRealIdForSession.mockImplementation(
      (sessionId: string) => sessionId,
    );
    mocks.getContextUsage.mockReset();
    mocks.getContextUsage.mockResolvedValue({ available: false });
    mocks.sessions = [
      {
        id: "chat-1",
        realId: "chat-1",
        sessionId: "chat-1",
        name: "会话 1",
        messages: [],
        meta: { plan_mode_enabled: true },
      },
    ];
    mocks.navigationSessionId = null;
    mocks.navigationTaskId = null;
    mocks.navigate.mockReset();
    mocks.createChat.mockClear();
    mocks.listCronJobs.mockReset();
    mocks.listCronJobs.mockResolvedValue([]);
    mocks.listExperts.mockReset();
    mocks.listExperts.mockResolvedValue([]);
    mocks.loadActiveModelData.mockClear();
    mocks.expertSelectorProps = null;
    mocks.setLoading.mockReset();
    mocks.getLoading.mockReset();
    mocks.getLoading.mockReturnValue(false);
    mocks.setSessionLoading.mockReset();
    mocks.setSessions.mockReset();
    mocks.updateChat.mockClear();
    mocks.updateSession.mockClear();
    mocks.clearNavigationParams.mockReset();
    if (mocks.sessionApi) {
      mocks.sessionApi.preferredChatId = "";
      mocks.sessionApi.onSessionCreated = null;
      mocks.sessionApi.onSessionIdResolved = null;
      mocks.sessionApi.onSessionRemoved = null;
      mocks.sessionApi.onSessionSelected = null;
      mocks.sessionApi.getSessionList.mockClear();
    }
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it.each([true, false])(
    "新版手动记录存在=%s 时正确判断是否尚未启动",
    async (hasRun) => {
      vi.spyOn(schedulerCronApi, "listAll").mockImplementation(
        async (context) => [
          {
            id: "manual-current",
            name: "Manual Current",
            enabled: true,
            tenant_id: context.tenant_id,
            source_id: context.source_id,
            agent_id: context.agent_id,
            management_owner: "scheduler",
            definition_version: 1,
            management_version: 1,
            management_status: "ready",
            dispatch_state: "ordinary",
            deleted: false,
            schedule: {
              type: "cron",
              cron: "0 9 * * *",
              timezone: "Asia/Shanghai",
            },
            task_type: "text",
            text: "手动结果",
            dispatch: {
              type: "channel",
              channel: "console",
              target: { user_id: context.actor_id, session_id: "chat-1" },
            },
            state: {
              last_run_at: hasRun ? "2026-10-19T05:18:00Z" : null,
              last_status: hasRun ? "success" : "idle",
            },
            task: {
              visible_in_my_tasks: true,
              chat_id: "chat-1",
              is_paused: false,
              is_running: false,
              has_scheduled_result: false,
              latest_scheduled_preview: "",
              unread_execution_count: 0,
            },
          } as ManagedJob,
        ],
      );
      render(<ChatPage />);
      await screen.findByRole("button", { name: "Open Manual Current" });
      if (hasRun)
        expect(mocks.taskInfo).not.toHaveBeenCalledWith(
          "当前任务暂未启动，等下次收到提醒再来看看哟~",
        );
      else
        expect(mocks.taskInfo).toHaveBeenCalledWith(
          "当前任务暂未启动，等下次收到提醒再来看看哟~",
        );
    },
  );

  it("renders the subagent monitor outside the composer beforeUI", () => {
    render(<ChatPage />);

    const monitor = screen.getByTestId("subagent-run-monitor");
    expect(screen.getByTestId("chat-sender-before-ui")).not.toContainElement(
      monitor,
    );
  });

  it("resolves the backend chat id and renders context usage in both composer prefixes", async () => {
    mocks.pathname = "/chat/temp-1";
    mocks.currentSessionId = "temp-1";
    mocks.getChatIdForSession.mockImplementation((sessionId: string) =>
      sessionId === "temp-1" ? "backend-chat-1" : sessionId,
    );

    render(<ChatPage />);

    expect(screen.getByTestId("chat-sender-prefix")).toContainElement(
      screen.getAllByRole("button", { name: /上下文占用/ })[1],
    );
    expect(screen.getByTestId("chat-welcome")).toContainElement(
      screen.getAllByRole("button", { name: /上下文占用/ })[0],
    );
    await waitFor(() => {
      expect(mocks.getContextUsage).toHaveBeenCalledWith("backend-chat-1");
      expect(mocks.getContextUsage).toHaveBeenCalledTimes(1);
    });

    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "backend-chat-1" },
      }),
    );
    await waitFor(() => {
      expect(mocks.getContextUsage).toHaveBeenCalledTimes(2);
    });

    window.dispatchEvent(new CustomEvent("model-switched"));
    await waitFor(() => {
      expect(mocks.getContextUsage).toHaveBeenCalledTimes(3);
    });
  });

  it("does not show the previous active chat usage while a route chat is unresolved", async () => {
    mocks.pathname = "/chat/1780458341751000";
    mocks.currentSessionId = "chat-1";
    mocks.getChatIdForSession.mockImplementation((sessionId: string) =>
      sessionId === "chat-1" ? "backend-chat-1" : null,
    );

    render(<ChatPage />);

    screen.getAllByRole("button", { name: /上下文占用.*暂无数据/ });
    await act(async () => Promise.resolve());
    expect(mocks.getContextUsage).not.toHaveBeenCalled();
  });

  it("keeps the subagent monitor mounted in content-only mode", () => {
    mocks.showContentOnly = true;

    render(<ChatPage />);

    expect(screen.getByTestId("subagent-run-monitor")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-sender-before-ui")).toBeNull();
  });

  it("disables active Plan Mode buttons when the composer is disabled", async () => {
    render(<ChatPage />);

    const buttons = screen.getAllByRole("button", { name: "计划模式" });

    expect(buttons).toHaveLength(2);
    buttons.forEach((button) => {
      expect(button).toBeDisabled();
    });

    fireEvent.click(buttons[0]);
    await Promise.resolve();

    expect(mocks.updateChat).not.toHaveBeenCalled();
  });

  it("disables the quick menu Plan Mode switch when the composer is disabled", () => {
    render(<ChatPage />);

    expect(screen.getByRole("switch", { name: "计划模式" })).toBeDisabled();
  });

  it("renders Plan Mode as compact text inside the Mode submenu", () => {
    mocks.sessions = [
      {
        id: "chat-1",
        realId: "chat-1",
        sessionId: "chat-1",
        name: "会话 1",
        messages: [],
        meta: { plan_mode_enabled: false },
      },
    ];

    render(<ChatPage />);

    expect(screen.getByText("模式")).toBeInTheDocument();
    expect(screen.getByText("计划")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "计划模式" })).toBeDisabled();
  });

  it("places Expert immediately after Mode in Composer quick actions", () => {
    render(<ChatPage />);

    const quickMenuItems = React.Children.toArray(
      mocks.capturedOptions?.sender?.quickMenuItems,
    ) as Array<
      React.ReactElement<{ children?: React.ReactNode; label: string }>
    >;
    const modeItems = React.Children.toArray(
      quickMenuItems[0].props.children,
    ) as Array<React.ReactElement<{ icon?: React.ReactNode; label: string }>>;
    const goalModeItem = modeItems.find((item) => item.props.label === "目标");
    const expertSelector = quickMenuItems[1].props
      .children as React.ReactElement<{ inline?: boolean }>;

    expect(quickMenuItems.map((item) => item.props.label)).toEqual([
      "模式",
      "专家",
    ]);
    expect(expertSelector.props.inline).toBe(true);
    expect(goalModeItem).toBeDefined();
    expect(goalModeItem?.props.icon).toBeUndefined();
  });

  it("keeps Goal Mode selected after submitting a goal request", async () => {
    mocks.inputDisabled = false;
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response("", { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<ChatPage />);

    const quickMenuItems = React.Children.toArray(
      mocks.capturedOptions?.sender?.quickMenuItems,
    ) as Array<React.ReactElement<{ children?: React.ReactNode }>>;
    const modeItems = React.Children.toArray(
      quickMenuItems[0].props.children,
    ) as Array<
      React.ReactElement<{
        label?: string;
        extra?: React.ReactElement<{ onChange: (enabled: boolean) => void }>;
      }>
    >;
    const goalModeItem = modeItems.find((item) => item.props.label === "目标");

    act(() => {
      goalModeItem?.props.extra?.props.onChange(true);
    });
    await waitFor(() => {
      const buttons = screen.getAllByRole("button", { name: "目标" });
      expect(buttons).toHaveLength(2);
      buttons.forEach((button) => expect(button).toBeEnabled());
    });

    await act(async () => {
      await mocks.capturedOptions?.api.fetch({
        input: [
          {
            role: "user",
            content: "起草一个可确认的目标",
            session: {},
          },
        ],
        session_id: "chat-1",
      });
    });

    const chatRequest = fetchMock.mock.calls.find(
      ([url]) => url === "/console/chat",
    );
    expect(chatRequest).toBeDefined();
    const requestBody = chatRequest?.[1]?.body;
    expect(typeof requestBody).toBe("string");
    expect(JSON.parse(requestBody as string)).toMatchObject({
      goal_mode_enabled: true,
    });
    screen
      .getAllByRole("button", { name: "目标" })
      .forEach((button) => expect(button).toBeEnabled());
  });

  it("hides Expert from Composer quick actions when no selectable experts are configured", async () => {
    render(<ChatPage />);

    await waitFor(() => {
      expect(mocks.listExperts).toHaveBeenCalledTimes(1);
      const quickMenuItems = React.Children.toArray(
        mocks.capturedOptions?.sender?.quickMenuItems,
      ) as Array<React.ReactElement<{ label: string }>>;

      expect(quickMenuItems.map((item) => item.props.label)).toEqual(["模式"]);
    });
  });

  it("shows the selected expert name above the composer", async () => {
    mocks.inputDisabled = false;
    mocks.listExperts.mockResolvedValue([
      {
        definition_id: "expert-1",
        enabled: true,
        valid: true,
        definition: {
          name: "专家一号",
          description: "处理复杂问题",
        },
      },
    ]);

    render(<ChatPage />);

    await waitFor(() => {
      expect(mocks.expertSelectorProps).toEqual(
        expect.objectContaining({
          experts: [
            expect.objectContaining({ id: "expert-1", name: "专家一号" }),
          ],
        }),
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "选择专家" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "专家一号" })).toBeEnabled();
      const quickMenuItems = React.Children.toArray(
        mocks.capturedOptions?.sender?.quickMenuItems,
      ) as Array<React.ReactElement<{ icon?: React.ReactNode; label: string }>>;
      const expertItem = quickMenuItems.find(
        (item) => item.props.label === "专家",
      );
      expect(expertItem?.props.icon).toBeDefined();
    });
  });

  it("creates a backend chat before persisting Plan Mode for a pending local session", async () => {
    mocks.inputDisabled = false;
    mocks.pathname = "/chat/1780458341751000";
    mocks.currentSessionId = "1780458341751000";
    mocks.getChatIdForSession.mockImplementation(() => null);
    mocks.getRealIdForSession.mockImplementation(() => null);
    mocks.sessions = [
      {
        id: "1780458341751000",
        realId: "",
        sessionId: "1780458341751000",
        name: "新会话",
        messages: [],
        meta: { plan_mode_enabled: false },
      },
    ];
    mocks.updateChat.mockResolvedValueOnce({
      meta: { plan_mode_enabled: true },
    });

    render(<ChatPage />);

    fireEvent.click(screen.getByRole("switch", { name: "计划模式" }));

    await waitFor(() => {
      expect(mocks.createChat).toHaveBeenCalledTimes(1);
    });

    expect(mocks.createChat).toHaveBeenCalledWith(
      expect.objectContaining({
        session_id: "1780458341751000",
        name: "新会话",
      }),
    );
    expect(mocks.updateChat).toHaveBeenCalledWith("chat-real-created", {
      meta: { plan_mode_enabled: true },
    });
  });

  it("keeps Plan Mode active when a pending local session resolves to a backend chat", async () => {
    mocks.inputDisabled = false;
    mocks.pathname = "/chat/1780458341751000";
    mocks.currentSessionId = "1780458341751000";
    mocks.getChatIdForSession.mockImplementation((sessionId: string) =>
      sessionId === "chat-real-created" ? sessionId : null,
    );
    mocks.getRealIdForSession.mockImplementation(() => null);
    mocks.sessions = [
      {
        id: "1780458341751000",
        realId: "",
        sessionId: "1780458341751000",
        name: "新会话",
        messages: [],
        meta: { plan_mode_enabled: false },
      },
    ];
    let resolveUpdateChat: (value: {
      meta: { plan_mode_enabled: boolean };
    }) => void = () => undefined;
    mocks.updateChat.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveUpdateChat = resolve;
        }),
    );

    const { rerender } = render(<ChatPage />);

    await waitFor(() => {
      expect(mocks.sessionApi?.onSessionIdResolved).toEqual(
        expect.any(Function),
      );
    });

    fireEvent.click(screen.getByRole("switch", { name: "计划模式" }));

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    });

    await act(async () => {
      mocks.sessionApi?.onSessionIdResolved?.(
        "1780458341751000",
        "chat-real-created",
      );
    });
    mocks.pathname = "/chat/chat-real-created";
    rerender(<ChatPage />);

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 220));
    });

    expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(
      screen.getAllByRole("button", { name: "计划模式" }).length,
    ).toBeGreaterThan(0);

    await act(async () => {
      resolveUpdateChat({ meta: { plan_mode_enabled: true } });
    });
  });

  it("keeps first-click Plan Mode visible when a new session id appears after the URL was cleared", async () => {
    mocks.inputDisabled = false;
    mocks.pathname = "/chat";
    mocks.currentSessionId = "";
    mocks.getChatIdForSession.mockImplementation(() => null);
    mocks.getRealIdForSession.mockImplementation(() => null);
    mocks.sessions = [];
    let resolveUpdateChat: (value: {
      meta: { plan_mode_enabled: boolean };
    }) => void = () => undefined;
    mocks.updateChat.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveUpdateChat = resolve;
        }),
    );

    const { rerender } = render(<ChatPage />);

    fireEvent.click(screen.getByRole("switch", { name: "计划模式" }));

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    });

    mocks.pathname = "/chat/1780458341751000";
    mocks.currentSessionId = "1780458341751000";
    mocks.sessions = [
      {
        id: "1780458341751000",
        realId: "",
        sessionId: "1780458341751000",
        name: "新会话",
        messages: [],
        meta: { plan_mode_enabled: false },
      },
    ];
    rerender(<ChatPage />);

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    });
    expect(
      screen.getAllByRole("button", { name: "计划模式" }).length,
    ).toBeGreaterThan(0);

    await act(async () => {
      resolveUpdateChat({ meta: { plan_mode_enabled: true } });
    });
  });

  it("keeps Plan Mode enabled after a blank-scope persist resolves before the new session id is available", async () => {
    mocks.inputDisabled = false;
    mocks.pathname = "/chat";
    mocks.currentSessionId = "";
    mocks.getChatIdForSession.mockImplementation(() => null);
    mocks.getRealIdForSession.mockImplementation(() => null);
    mocks.sessions = [];
    let resolveUpdateChat: (value: {
      meta: { plan_mode_enabled: boolean };
    }) => void = () => undefined;
    let resolveUpdateSession: (value: unknown) => void = () => undefined;
    mocks.updateChat.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveUpdateChat = resolve;
        }),
    );
    mocks.updateSession.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveUpdateSession = resolve;
        }),
    );

    const { rerender } = render(<ChatPage />);

    fireEvent.click(screen.getByRole("switch", { name: "计划模式" }));

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    });

    await act(async () => {
      resolveUpdateChat({ meta: { plan_mode_enabled: true } });
      resolveUpdateSession(undefined);
    });

    mocks.pathname = "/chat/chat-real-created";
    mocks.currentSessionId = "chat-real-created";
    mocks.sessions = [
      {
        id: "chat-real-created",
        realId: "chat-real-created",
        sessionId: "chat-real-created",
        name: "新会话",
        messages: [],
        meta: { plan_mode_enabled: false },
      },
    ];
    rerender(<ChatPage />);

    expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("keeps Plan Mode enabled when the first submitted message creates a temp session id", async () => {
    mocks.inputDisabled = false;
    mocks.pathname = "/chat";
    mocks.currentSessionId = "";
    mocks.getChatIdForSession.mockImplementation(() => null);
    mocks.getRealIdForSession.mockImplementation(() => null);
    mocks.sessions = [];
    mocks.updateChat.mockResolvedValue({ meta: { plan_mode_enabled: true } });
    mocks.updateSession.mockResolvedValue([]);

    const { rerender } = render(<ChatPage />);

    fireEvent.click(screen.getByRole("switch", { name: "计划模式" }));

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    });

    mocks.currentSessionId = "1780458341751000";
    mocks.sessions = [
      {
        id: "1780458341751000",
        realId: "",
        sessionId: "1780458341751000",
        name: "新会话",
        messages: [],
        meta: { plan_mode_enabled: false },
      },
    ];
    rerender(<ChatPage />);

    expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("does not render Plan Mode disabled after the first enable while a new session scope appears", async () => {
    mocks.inputDisabled = false;
    mocks.pathname = "/chat";
    mocks.currentSessionId = "";
    mocks.getChatIdForSession.mockImplementation(() => null);
    mocks.getRealIdForSession.mockImplementation(() => null);
    mocks.sessions = [];
    let resolveUpdateChat: (value: {
      meta: { plan_mode_enabled: boolean };
    }) => void = () => undefined;
    mocks.updateChat.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveUpdateChat = resolve;
        }),
    );

    const { rerender } = render(<ChatPage />);

    fireEvent.click(screen.getByRole("switch", { name: "计划模式" }));

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    });
    const firstEnabledRender = mocks.planModeEnabledHistory.indexOf(true);
    expect(firstEnabledRender).toBeGreaterThanOrEqual(0);

    mocks.currentSessionId = "1780458341751000";
    mocks.sessions = [
      {
        id: "1780458341751000",
        realId: "",
        sessionId: "1780458341751000",
        name: "新会话",
        messages: [],
        meta: { plan_mode_enabled: false },
      },
    ];
    rerender(<ChatPage />);

    expect(
      mocks.planModeEnabledHistory.slice(firstEnabledRender),
    ).not.toContain(false);

    await act(async () => {
      resolveUpdateChat({ meta: { plan_mode_enabled: true } });
    });
  });

  it("syncs persisted Plan Mode metadata back to the session context", async () => {
    mocks.inputDisabled = false;
    mocks.sessions = [
      {
        id: "chat-1",
        realId: "chat-1",
        sessionId: "chat-1",
        name: "会话 1",
        messages: [],
        meta: { plan_mode_enabled: false },
      },
    ];
    const updatedSessions = [
      {
        ...mocks.sessions[0],
        meta: { plan_mode_enabled: true },
      },
    ];
    mocks.updateChat.mockResolvedValueOnce({
      meta: { plan_mode_enabled: true },
    });
    mocks.updateSession.mockResolvedValueOnce(updatedSessions);

    render(<ChatPage />);

    fireEvent.click(screen.getByRole("switch", { name: "计划模式" }));

    await waitFor(() => {
      expect(mocks.setSessions).toHaveBeenCalledWith(updatedSessions);
    });
  });

  it("uses the route chat id instead of the previous active session for Plan Mode", () => {
    mocks.inputDisabled = false;
    mocks.pathname = "/chat/chat-2";
    mocks.currentSessionId = "chat-1";
    mocks.sessions = [
      {
        id: "chat-1",
        realId: "chat-1",
        sessionId: "chat-1",
        name: "会话 1",
        messages: [],
        meta: { plan_mode_enabled: true },
      },
      {
        id: "chat-2",
        realId: "chat-2",
        sessionId: "chat-2",
        name: "会话 2",
        messages: [],
        meta: { plan_mode_enabled: false },
      },
    ];

    render(<ChatPage />);

    expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(
      screen.queryByRole("button", { name: "计划模式" }),
    ).not.toBeInTheDocument();
  });

  it("does not fall back to the previous active Plan Mode session when the route chat is not loaded yet", () => {
    mocks.inputDisabled = false;
    mocks.pathname = "/chat/chat-2";
    mocks.currentSessionId = "chat-1";
    mocks.sessions = [
      {
        id: "chat-1",
        realId: "chat-1",
        sessionId: "chat-1",
        name: "会话 1",
        messages: [],
        meta: { plan_mode_enabled: true },
      },
    ];

    render(<ChatPage />);

    expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(
      screen.queryByRole("button", { name: "计划模式" }),
    ).not.toBeInTheDocument();
  });

  it("removes active Plan Mode controls after switching from an enabled chat to a disabled chat", async () => {
    mocks.inputDisabled = false;
    mocks.pathname = "/chat/chat-1";
    mocks.currentSessionId = "chat-1";
    mocks.sessions = [
      {
        id: "chat-1",
        realId: "chat-1",
        sessionId: "chat-1",
        name: "会话 1",
        messages: [],
        meta: { plan_mode_enabled: true },
      },
      {
        id: "chat-2",
        realId: "chat-2",
        sessionId: "chat-2",
        name: "会话 2",
        messages: [],
        meta: { plan_mode_enabled: false },
      },
    ];

    const { rerender } = render(<ChatPage />);

    expect(
      screen.getAllByRole("button", { name: "计划模式" }).length,
    ).toBeGreaterThan(0);

    mocks.pathname = "/chat/chat-2";
    rerender(<ChatPage />);

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: "计划模式" })).toHaveAttribute(
        "aria-checked",
        "false",
      );
    });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 220));
    });

    expect(
      screen.queryByRole("button", { name: "计划模式" }),
    ).not.toBeInTheDocument();
  });

  it("renders the active plan interaction card as the sender composer replacement", () => {
    render(<ChatPage />);

    expect(mocks.capturedOptions?.sender?.renderComposer).toEqual(
      expect.any(Function),
    );
    expect(
      screen.queryByTestId("active-plan-review-card"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId("active-plan-interaction-composer"),
    ).toBeInTheDocument();
  });

  it("defers plan review rendering to the response card", () => {
    render(<ChatPage />);

    const renderer = mocks.capturedOptions?.cards?.PlanInteraction;

    render(
      <>
        {renderer?.({
          data: {
            card_type: "plan_review",
            plan_id: "plan-123",
            title: "Implementation plan",
            summary: "Plan summary",
            steps: [],
            risks: [],
            verification: [],
          },
        })}
      </>,
    );

    expect(
      screen.queryByTestId("plan-review-message-card"),
    ).not.toBeInTheDocument();
  });

  it("defers Continue modifying and sends the next submission as plan revision feedback", async () => {
    render(<ChatPage />);

    fireEvent.click(screen.getByRole("button", { name: "Continue modifying" }));

    await waitFor(() => {
      expect(mocks.capturedOptions?.sender?.beforeSubmit).toBeDefined();
    });

    const result = await mocks.capturedOptions?.sender?.beforeSubmit({
      query: "Narrow the implementation scope",
      fileList: [],
      biz_params: {},
    });

    expect(result).toMatchObject({
      query: "Narrow the implementation scope",
      biz_params: {
        mode: "plan",
        plan_interaction_response: {
          card_type: "plan_review",
          plan_id: "plan-123",
          decision: "revise",
          feedback: "Narrow the implementation scope",
        },
      },
    });
  });

  it("preserves an explicit plan interaction response after Continue modifying and clears the pending revision", async () => {
    render(<ChatPage />);

    fireEvent.click(screen.getByRole("button", { name: "Continue modifying" }));

    await waitFor(() => {
      expect(mocks.capturedOptions?.sender?.beforeSubmit).toBeDefined();
    });

    const explicitResponse = {
      card_type: "plan_review",
      plan_id: "plan-123",
      decision: "execute",
    };
    const explicitResult = await mocks.capturedOptions?.sender?.beforeSubmit({
      query: "Execute plan plan-123",
      fileList: [],
      biz_params: {
        mode: "normal",
        plan_interaction_response: explicitResponse,
      },
    });

    expect(explicitResult).toMatchObject({
      query: "Execute plan plan-123",
      biz_params: {
        mode: "normal",
        plan_interaction_response: explicitResponse,
      },
    });

    const ordinaryResult = await mocks.capturedOptions?.sender?.beforeSubmit({
      query: "Ordinary follow up",
      fileList: [],
      biz_params: {},
    });

    expect(ordinaryResult).toMatchObject({
      query: "Ordinary follow up",
      biz_params: {
        mode: "plan",
      },
    });
    expect(
      ordinaryResult?.biz_params?.plan_interaction_response,
    ).toBeUndefined();
  });

  it("clears pending revision when Exit Plan Mode is clicked after Continue modifying", async () => {
    render(<ChatPage />);

    fireEvent.click(screen.getByRole("button", { name: "Continue modifying" }));
    fireEvent.click(screen.getByRole("button", { name: "Exit Plan Mode" }));

    await waitFor(() => {
      expect(mocks.updateChat).toHaveBeenCalledWith("chat-1", {
        meta: { plan_mode_enabled: false },
      });
    });

    const result = await mocks.capturedOptions?.sender?.beforeSubmit({
      query: "Ordinary follow up",
      fileList: [],
      biz_params: {},
    });

    expect(result).toMatchObject({
      query: "Ordinary follow up",
    });
    expect(result?.biz_params?.plan_interaction_response).toBeUndefined();
  });

  it("clears pending revision when the active Plan Mode control is disabled after Continue modifying", async () => {
    mocks.inputDisabled = false;
    render(<ChatPage />);

    fireEvent.click(screen.getByRole("button", { name: "Continue modifying" }));
    fireEvent.click(screen.getAllByRole("button", { name: "计划模式" })[0]);

    await waitFor(() => {
      expect(mocks.updateChat).toHaveBeenCalledWith("chat-1", {
        meta: { plan_mode_enabled: false },
      });
    });

    const result = await mocks.capturedOptions?.sender?.beforeSubmit({
      query: "Ordinary follow up",
      fileList: [],
      biz_params: {},
    });

    expect(result).toMatchObject({
      query: "Ordinary follow up",
    });
    expect(result?.biz_params?.plan_interaction_response).toBeUndefined();
  });

  it("does not clear or replace composer input when Continue modifying is clicked", () => {
    const setContentHandler = vi.fn();
    document.addEventListener(
      "agentscope-runtime:set-input-content",
      setContentHandler,
    );

    try {
      render(<ChatPage />);

      fireEvent.click(
        screen.getByRole("button", { name: "Continue modifying" }),
      );

      expect(setContentHandler).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener(
        "agentscope-runtime:set-input-content",
        setContentHandler,
      );
    }
  });

  it("blocks an empty submission after Continue modifying", async () => {
    render(<ChatPage />);

    fireEvent.click(screen.getByRole("button", { name: "Continue modifying" }));

    const result = await mocks.capturedOptions?.sender?.beforeSubmit({
      query: "   ",
      fileList: [],
      biz_params: {},
    });

    expect(result).toBe(false);
  });

  it("passes a Plan Mode decision callback that can close local Plan Mode state", async () => {
    render(<ChatPage />);

    fireEvent.click(screen.getByRole("button", { name: "Exit Plan Mode" }));

    await waitFor(() => {
      expect(mocks.updateChat).toHaveBeenCalledWith("chat-1", {
        meta: { plan_mode_enabled: false },
      });
    });
  });

  it("triggers HTML auto preview when taskId navigation resolves to a chat", async () => {
    mocks.navigationTaskId = "task-from-url";
    mocks.listCronJobs.mockResolvedValue([
      {
        id: "task-from-url",
        name: "URL Task",
        enabled: true,
        schedule: { type: "cron", cron: "* * * * *" },
        dispatch: {
          type: "channel",
          target: { user_id: "test-user", session_id: "chat-2" },
        },
        task: {
          visible_in_my_tasks: true,
          chat_id: "chat-2",
          has_scheduled_result: true,
          latest_scheduled_preview: "",
          unread_execution_count: 0,
          is_running: false,
        },
      },
    ]);

    render(<ChatPage />);

    await screen.findByRole("button", { name: "Open URL Task" });

    expect(mocks.clearNavigationParams).toHaveBeenCalled();
    await waitFor(() => {
      expect(mocks.navigate).toHaveBeenCalledWith("/chat/chat-2", {
        replace: true,
      });
    });
    expect(screen.getByTestId("auto-preview-provider")).toHaveAttribute(
      "data-trigger-key",
      "1",
    );
  });

  it("wires task tracking context and triggers auto preview from sidebar task open", async () => {
    mocks.listCronJobs.mockResolvedValue([
      {
        id: "task-current",
        name: "Current Task",
        enabled: true,
        schedule: { type: "cron", cron: "* * * * *" },
        dispatch: {
          type: "channel",
          target: { user_id: "test-user", session_id: "chat-1" },
        },
        task: {
          visible_in_my_tasks: true,
          chat_id: "chat-1",
          has_scheduled_result: true,
          latest_scheduled_preview: "",
          unread_execution_count: 0,
          is_running: false,
        },
      },
      {
        id: "task-other",
        name: "Other Task",
        enabled: true,
        schedule: { type: "cron", cron: "* * * * *" },
        dispatch: {
          type: "channel",
          target: { user_id: "test-user", session_id: "chat-2" },
        },
        task: {
          visible_in_my_tasks: true,
          chat_id: "chat-2",
          has_scheduled_result: true,
          latest_scheduled_preview: "",
          unread_execution_count: 0,
          is_running: false,
        },
      },
    ]);

    render(<ChatPage />);

    await waitFor(() => {
      expect(
        screen.getByTestId("html-preview-tracking-provider"),
      ).toHaveAttribute("data-cron-task-id", "task-current");
    });
    expect(
      screen.getByTestId("html-preview-tracking-provider"),
    ).toHaveAttribute("data-cron-task-name", "Current Task");

    fireEvent.click(screen.getByRole("button", { name: "Open Other Task" }));

    expect(mocks.setSessionLoading).toHaveBeenCalledWith(true);
    expect(mocks.navigate).toHaveBeenCalledWith("/chat/chat-2", {
      replace: true,
    });
    expect(screen.getByTestId("auto-preview-provider")).toHaveAttribute(
      "data-trigger-key",
      "1",
    );
  });
});
