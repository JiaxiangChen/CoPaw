// ==================== 组件引入方式变更 (Kun He) ====================
import {
  AgentScopeRuntimeWebUILayout,
  AgentScopeRuntimeWebUIComposedProvider,
  IAgentScopeRuntimeWebUIOptions,
  type IAgentScopeRuntimeWebUISenderOptions,
  type IAgentScopeRuntimeWebUIRef,
  type IChatInputProps,
  useChatAnywhereSessions,
  useChatAnywhereSessionsState,
} from "@/components/agentscope-chat";
import AgentScopeRuntimeRequestCard from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Request/Card";
import AgentScopeRuntimeResponseCard from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Card";
import ConversationCompactionBoundary from "./components/ConversationCompactionBoundary";
import ContextUsageIndicator from "./components/ContextUsageIndicator";
import { useContextUsageController } from "./components/ContextUsageIndicator/useContextUsageController";
// ==================== 组件引入方式变更结束 ====================
import {
  Children,
  cloneElement,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useTransition,
} from "react";
import { flushSync } from "react-dom";
import { Button, Modal, Result, Switch } from "antd";
import { useAppMessage } from "../../hooks/useAppMessage";
import {
  ControlOutlined,
  ExclamationCircleOutlined,
  SettingOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { SparkCopyLine } from "@agentscope-ai/icons";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";
import sessionApi from "./sessionApi";
import defaultConfig, { getDefaultConfig } from "./OptionsPanel/defaultConfig";
import { chatApi } from "../../api/modules/chat";
import { cronJobApi } from "../../api/modules/cronjob";
import { feedbackApi } from "../../api/modules/feedback";
import { expertsApi, type Expert } from "../../api/modules/experts";
import { contextReferencesApi } from "../../api/modules/contextReferences";
import type { SkillMentionItem } from "../../components/agentscope-chat/SkillMentions/useSkillMentions";
import { getApiUrl } from "../../api/config";
import { buildAuthHeaders } from "../../api/authHeaders";
import type {
  ProviderInfo,
  ModelInfo,
  CronJobSpecOutput,
} from "../../api/types";
import type { FeedbackRecord } from "../../api/types/feedback";
import ModelSelector from "./ModelSelector";
import ExpertSelector from "./ExpertSelector";
import {
  normalizeSelectableExperts,
  resolveExpertLabel,
  type SelectableExpert,
} from "./expertSelection";
import { useTheme } from "../../contexts/ThemeContext";
import { useAgentStore } from "../../stores/agentStore";
import { useSourceSystemConfigStore } from "../../stores/sourceSystemConfigStore";
import { useProviderModelStore } from "../../stores/providerModelStore";
// ==================== 组件引入方式变更 (Kun He) ====================
import { useChatAnywhereInput } from "@/components/agentscope-chat";
import DragUploadOverlay from "@/components/agentscope-chat/DragUploadOverlay";
// ==================== 组件引入方式变更结束 ====================
// ==================== userId 统一整改 (Kun He) ====================
// 使用统一的 getUserId/getChannel helper
import { getUserId, getChannel } from "../../utils/identity";
// ==================== userId 统一整改结束 ====================
// ==================== 品牌主题 (Kun He) ====================
import { useBrandTheme } from "../../contexts/BrandThemeContext";
// ==================== 品牌主题结束 ====================
// ==================== URL 导航参数 (Kun He, 2026-04-15) ====================
import { useIframeStore } from "../../stores/iframeStore";
import { useChatPresentationStore } from "../../stores/chatPresentationStore";
// ==================== URL 导航参数结束 ====================
import styles from "./index.module.less";
import { Form } from "@agentscope-ai/design";
// import ChatActionGroup from "./components/ChatActionGroup";
import ChatHeaderTitle from "./components/ChatHeaderTitle";
import ChatActionGroup from "./components/ChatActionGroup";
import { ChatShareSelectionProvider } from "./chatShareContext";
import ChatSessionInitializer from "./components/ChatSessionInitializer";
import SubAgentRunMonitor from "./components/SubAgentRunMonitor";
import GoalMonitor from "./components/GoalMonitor";
import ConversationQuickNav from "@/components/ConversationQuickNav";
// ==================== 首页改版 (Kun He) ====================
import WelcomeCenterLayout from "@/components/agentscope-chat/WelcomeCenterLayout";
import ChatSidebar from "./components/ChatSidebar";
import { createWelcomeSkillMentions } from "./welcomeSkillMentions";
import { selectContextReferences } from "./contextReferenceDefaults";
// ==================== 首页改版结束 ====================
// ==================== 自定义工具渲染器 (customToolRenderConfig) ====================
import CopyFileToStatic from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/customToolRenders/CopyFileToStatic";
// ==================== 自定义工具渲染器结束 ====================
import {
  toDisplayUrl,
  copyText,
  extractCopyableText,
  buildModelError,
  normalizeContentUrls,
  extractUserMessageText,
  type CopyableResponse,
  type RuntimeLoadingBridgeApi,
} from "./utils";
import {
  deriveChatTaskState,
  getTaskOpenTarget,
  shouldMarkTaskReadOnOpen,
} from "./taskJobs";
import { DEFAULT_FORM_VALUES } from "../Control/CronJobs/components";
import { buildCronJobFormValues } from "../Control/CronJobs/helpers";
import {
  extractTaskContentText,
  submitCronTaskEdit,
  type CronTaskEditFormValues,
} from "./taskEditSubmit";
import ChatTaskEditFormBody from "./components/ChatTaskEditFormBody";
import { shouldRefreshCurrentTaskMessages } from "./taskMessageRefresh";
import { resolveCurrentFileUrlNetwork } from "./fileUrlNetwork";
import { shouldClearPendingScenarioPreset } from "./scenarioPresetRequest";
import { matchesResolvedChatId } from "./sessionApi/resolvedSessionMapping";
import {
  CHAT_ATTACHMENT_ACCEPT_HINT,
  uploadChatAttachment,
} from "./attachmentUploadPolicy";
import {
  ComposerQuickMenuItem,
  ComposerQuickMenuSubmenu,
} from "@/components/agentscope-chat/ComposerQuickMenu";
import { emit } from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Context/useChatAnywhereEventEmitter";

import RuntimeRequestCard from "./components/RuntimeRequestCard";
import { FOLLOW_UP_SUBMIT_FAILED_EVENT } from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/followUpSubmit";
import { createChatStreamAbortReason } from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/abortReasons";
import RuntimeResponseCard, {
  RuntimeResponseFeedbackCard,
} from "./components/RuntimeResponseCard";
import { isResponseFeedbackUserAllowed } from "./components/ResponseFeedbackCard/whitelist";
import ApprovalActionCard from "./components/ApprovalActionCard";
import WPlusSopActiveBar from "./components/WPlusSopActiveBar";
import WPlusSopEntryCard from "./components/WPlusSopEntryCard";
import { ActivePlanInteractionComposer } from "./components/PlanInteractionCards";
import TaskRunGroupCard from "./components/TaskRunGroupCard";
import TaskProgressFloatingCard from "./components/TaskProgressFloatingCard";
import {
  ActivePlanModeButton,
  PlanModeMenuItem,
  getPlanModeEnabled,
  getPlanModeForRequest,
  getScopedPlanModeEnabled,
  persistPlanModeState,
  preparePlanModeSubmit,
  resolveActivePlanModeSession,
  isPlanModeSubmitCancelled,
  type PlanModeLocalState,
  type PlanModeSessionLike,
} from "./planMode";
import FileManager from "./components/FileManager";
import { ChatAutoPreviewHtmlProvider as AutoPreviewHtmlProvider } from "@/components/agentscope-chat/ChatAutoPreviewHtmlProvider";
import { HtmlPreviewTrackingProvider } from "@/components/agentscope-chat/HtmlPreviewTrackingContext";
import { ChatContentOnlyProvider } from "@/components/agentscope-chat/ChatContentOnlyContext";
import type {
  ChatApprovalActionCardData,
  ChatPlanReviewCardData,
  ChatRuntimeRequestCardData,
  ChatRuntimeResponseCardData,
  ChatTaskRunGroupCardData,
} from "./messageMeta";
import type { WPlusSopEntryProposal } from "@/api/types/wplusSop";
import {
  buildFeedbackLookup,
  collectFeedbackResponsesFromMessages,
  findFeedbackForResponse,
  type FeedbackLookupMap,
} from "./feedbackLookup";
import {
  ChatFeedbackRenderProvider,
  useChatFeedbackRenderContext,
  type ChatFeedbackRenderContextValue,
} from "./feedbackRenderContext";
import {
  ChatPlanReviewRenderProvider,
  type ChatPlanReviewRenderContextValue,
} from "./planReviewRenderContext";
import {
  CHAT_TASK_PROGRESS_UPDATE_EVENT,
  isTaskProgressUpdateForActiveSession,
  normalizeTaskProgressUpdateEventDetail,
  type ChatTaskProgressData,
  type ChatTaskProgressUpdateDetail,
} from "./taskProgressEvents";
import { isChatTaskProgressEnabled } from "./taskProgressConfig";
import GlobalVoiceRecorder from "@/components/GlobalVoiceRecorder";
import { shouldShowGlobalVoiceRecorder } from "@/components/GlobalVoiceRecorder/presentation";
import { shouldRouteGoalRequestAsSteering } from "./goalSteeringRouting";
import { FilePreviewPresentationProvider } from "@/components/agentscope-chat/FilePreviewPresentationContext";

const CHAT_ATTACHMENT_MAX_MB = 10;
const TASK_RUNNING_POLL_MS = 30_000;

function useExternalApprovalResolvedRefresh() {
  const { refreshSession } = useChatAnywhereSessions();
  return useCallback(() => {
    void refreshSession();
  }, [refreshSession]);
}

const chatCardRenderers = {
  ConversationCompactionBoundary,
  AgentScopeRuntimeRequestCard: (props: {
    data: ChatRuntimeRequestCardData;
  }) => <RuntimeRequestCard {...props} />,
  AgentScopeRuntimeResponseCard: (props: {
    data: ChatRuntimeResponseCardData;
    isLast?: boolean;
  }) => <RuntimeResponseCard {...props} />,
  ResponseFeedback: (props: { data: ChatRuntimeResponseCardData }) => {
    const feedback = useChatFeedbackRenderContext();
    return (
      <RuntimeResponseFeedbackCard
        {...props}
        chatId={feedback.feedbackChatId}
        existingFeedback={
          feedback.feedbackLookupPending
            ? null
            : findFeedbackForResponse(feedback.feedbackLookup, props.data)
        }
        loadingFeedback={feedback.feedbackLookupPending}
        onFeedbackSaved={feedback.onFeedbackSaved}
        sessionId={feedback.feedbackSessionId}
        task={feedback.feedbackTask}
      />
    );
  },
  ApprovalAction: (props: { data: ChatApprovalActionCardData }) => {
    const onExternalApprovalResolved = useExternalApprovalResolvedRefresh();
    return (
      <ApprovalActionCard
        {...props}
        onExternalApprovalResolved={onExternalApprovalResolved}
      />
    );
  },
  WPlusSopEntryProposal: (props: { data: WPlusSopEntryProposal }) => (
    <WPlusSopEntryCard {...props} />
  ),
  PlanInteraction: () => null,
  TaskRunGroupCard: (props: { data: ChatTaskRunGroupCardData }) => {
    const feedback = useChatFeedbackRenderContext();
    const onExternalApprovalResolved = useExternalApprovalResolvedRefresh();
    return (
      <TaskRunGroupCard
        {...props}
        chatId={feedback.feedbackChatId}
        feedbackLookup={feedback.feedbackLookup}
        loadingFeedback={feedback.feedbackLookupPending}
        onFeedbackSaved={feedback.onFeedbackSaved}
        onExternalApprovalResolved={onExternalApprovalResolved}
        sessionId={feedback.feedbackSessionId}
        task={feedback.feedbackTask}
      />
    );
  },
};
const TASK_PAGE_POLL_MS = 30_000;
const TASK_PENDING_POLL_MS = 30_000;

function createTimedAbortSignal(
  externalSignal?: AbortSignal,
  timeoutMs: number | null = null,
) {
  const controller = new AbortController();

  const abortWithReason = (reason?: unknown) => {
    if (controller.signal.aborted) return;
    controller.abort(
      reason ?? new DOMException("The operation was aborted.", "AbortError"),
    );
  };

  if (externalSignal?.aborted) {
    abortWithReason(externalSignal.reason);
  }

  const handleExternalAbort = () => {
    abortWithReason(externalSignal?.reason);
  };

  if (externalSignal && !externalSignal.aborted) {
    externalSignal.addEventListener("abort", handleExternalAbort, {
      once: true,
    });
  }

  const timeoutId =
    typeof timeoutMs === "number" && Number.isFinite(timeoutMs) && timeoutMs > 0
      ? window.setTimeout(() => {
          const elapsedSeconds = Math.ceil(timeoutMs / 1000);
          abortWithReason(
            createChatStreamAbortReason(
              "timeout",
              `任务执行超时（${elapsedSeconds}s），已自动终止。`,
            ),
          );
        }, timeoutMs)
      : undefined;

  return {
    signal: controller.signal,
    cleanup: () => {
      if (timeoutId !== undefined) {
        window.clearTimeout(timeoutId);
      }
      if (externalSignal) {
        externalSignal.removeEventListener("abort", handleExternalAbort);
      }
    },
  };
}

interface SessionInfo {
  session_id?: string;
  user_id?: string;
  channel?: string;
}

interface ChatRequestTarget {
  session_id?: string;
  logical_session_id?: string;
  chat_id?: string | null;
}

interface PlanModeSession extends PlanModeSessionLike {
  id?: string;
  realId?: string;
  sessionId?: string;
  session_id?: string;
  userId?: string;
  channel?: string;
  name?: string;
}

interface CustomWindow extends Window {
  currentSessionId?: string;
  currentUserId?: string;
  currentChannel?: string;
}

declare const window: CustomWindow;

interface CommandSuggestion {
  command: string;
  value: string;
  description: string;
}

type InputMessage = {
  role?: string;
  content?: unknown;
};

type PendingPlanRevision = {
  planId: string;
};

function renderSuggestionLabel(command: string, description: string) {
  return (
    <div className={styles.suggestionLabel}>
      <span className={styles.suggestionCommand}>{command}</span>
      <span className={styles.suggestionDescription}>{description}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// ==================== userId 统一整改 (Kun He) ====================
// DEFAULT_USER_ID 和 DEFAULT_CHANNEL 已移至 constants/identity.ts
// 通过 getUserId() 和 getChannel() 获取
// ==================== userId 统一整改结束 ====================

// ---------------------------------------------------------------------------
// Custom hooks
// ---------------------------------------------------------------------------

/** Handle IME composition events to prevent premature Enter key submission. */
function useIMEComposition(isChatActive: () => boolean) {
  const isComposingRef = useRef(false);

  useEffect(() => {
    const handleCompositionStart = () => {
      if (!isChatActive()) return;
      isComposingRef.current = true;
    };

    const handleCompositionEnd = () => {
      if (!isChatActive()) return;
      // Use a slightly longer delay for Safari on macOS, which fires keydown
      // after compositionend within the same event loop tick.
      setTimeout(() => {
        isComposingRef.current = false;
      }, 200);
    };

    const suppressImeEnter = (e: KeyboardEvent) => {
      if (!isChatActive()) return;
      const target = e.target as HTMLElement;
      if (
        (target?.tagName === "TEXTAREA" || target?.isContentEditable) &&
        e.key === "Enter" &&
        !e.shiftKey
      ) {
        // e.isComposing is the standard flag; isComposingRef covers the
        // post-compositionend grace period needed by Safari.
        if (isComposingRef.current || e.isComposing) {
          e.stopPropagation();
          e.stopImmediatePropagation();
          e.preventDefault();
          return false;
        }
      }
    };

    document.addEventListener("compositionstart", handleCompositionStart, true);
    document.addEventListener("compositionend", handleCompositionEnd, true);
    // Listen on both keydown (Safari) and keypress (legacy) in capture phase.
    document.addEventListener("keydown", suppressImeEnter, true);
    document.addEventListener("keypress", suppressImeEnter, true);

    return () => {
      document.removeEventListener(
        "compositionstart",
        handleCompositionStart,
        true,
      );
      document.removeEventListener(
        "compositionend",
        handleCompositionEnd,
        true,
      );
      document.removeEventListener("keydown", suppressImeEnter, true);
      document.removeEventListener("keypress", suppressImeEnter, true);
    };
  }, [isChatActive]);

  return isComposingRef;
}

/** Fetch and track multimodal capabilities for the active model. */
function useMultimodalCapabilities(
  modelRefreshKey: number,
  locationPathname: string,
  isChatActive: () => boolean,
) {
  const [multimodalCaps, setMultimodalCaps] = useState<{
    supportsMultimodal: boolean;
    supportsImage: boolean;
    supportsVideo: boolean;
  }>({ supportsMultimodal: false, supportsImage: false, supportsVideo: false });
  const loadModelData = useProviderModelStore((state) => state.loadModelData);

  const fetchMultimodalCaps = useCallback(async () => {
    try {
      const { providers, activeModels } = await loadModelData({
        scope: "effective",
      });
      const activeProviderId = activeModels?.active_llm?.provider_id;
      const activeModelId = activeModels?.active_llm?.model;
      if (!activeProviderId || !activeModelId) {
        setMultimodalCaps({
          supportsMultimodal: false,
          supportsImage: false,
          supportsVideo: false,
        });
        return;
      }
      const provider = (providers as ProviderInfo[]).find(
        (p) => p.id === activeProviderId,
      );
      if (!provider) {
        setMultimodalCaps({
          supportsMultimodal: false,
          supportsImage: false,
          supportsVideo: false,
        });
        return;
      }
      const allModels: ModelInfo[] = [
        ...(provider.models ?? []),
        ...(provider.extra_models ?? []),
      ];
      const model = allModels.find((m) => m.id === activeModelId);
      setMultimodalCaps({
        supportsMultimodal: model?.supports_multimodal ?? false,
        supportsImage: model?.supports_image ?? false,
        supportsVideo: model?.supports_video ?? false,
      });
    } catch {
      setMultimodalCaps({
        supportsMultimodal: false,
        supportsImage: false,
        supportsVideo: false,
      });
    }
  }, [loadModelData]);

  // Fetch caps on mount and whenever modelRefreshKey changes
  useEffect(() => {
    fetchMultimodalCaps();
  }, [fetchMultimodalCaps, modelRefreshKey]);

  // Also poll caps when navigating back to chat
  useEffect(() => {
    if (isChatActive()) {
      fetchMultimodalCaps();
    }
  }, [locationPathname, fetchMultimodalCaps, isChatActive]);

  // Listen for model-switched event from ModelSelector
  useEffect(() => {
    const handler = () => {
      fetchMultimodalCaps();
    };
    window.addEventListener("model-switched", handler);
    return () => window.removeEventListener("model-switched", handler);
  }, [fetchMultimodalCaps]);

  return multimodalCaps;
}

function RuntimeLoadingBridge({
  bridgeRef,
}: {
  bridgeRef: { current: RuntimeLoadingBridgeApi | null };
}) {
  const { setLoading, getLoading } = useChatAnywhereInput(
    (value) =>
      ({
        setLoading: value.setLoading,
        getLoading: value.getLoading,
      }) as RuntimeLoadingBridgeApi,
  );

  useEffect(() => {
    if (!setLoading || !getLoading) {
      bridgeRef.current = null;
      return;
    }

    bridgeRef.current = {
      setLoading,
      getLoading,
    };

    return () => {
      if (bridgeRef.current?.setLoading === setLoading) {
        bridgeRef.current = null;
      }
    };
  }, [getLoading, setLoading, bridgeRef]);

  return null;
}

function ActivePlanModeControl({
  enabled,
  label,
  displayLabel,
  onDisable,
}: {
  enabled: boolean;
  label: string;
  displayLabel?: string;
  onDisable: () => void;
}) {
  const inputState = useChatAnywhereInput((value) => ({
    disabled: Boolean(value.disabled),
  }));
  const disabled = Boolean(inputState.disabled);

  return (
    <ActivePlanModeButton
      enabled={enabled}
      disabled={disabled}
      label={label}
      displayLabel={displayLabel}
      onDisable={onDisable}
    />
  );
}

function ActiveGoalModeControl({
  enabled,
  onDisable,
}: {
  enabled: boolean;
  onDisable: () => void;
}) {
  const inputState = useChatAnywhereInput((value) => ({
    disabled: Boolean(value.disabled),
  }));

  return (
    <ActivePlanModeButton
      enabled={enabled}
      disabled={Boolean(inputState.disabled)}
      label="目标"
      showIcon={false}
      onDisable={onDisable}
    />
  );
}

function ActiveExpertControl({
  expert,
  onDisable,
}: {
  expert: SelectableExpert | null;
  onDisable: () => void;
}) {
  const inputState = useChatAnywhereInput((value) => ({
    disabled: Boolean(value.disabled),
  }));

  return (
    <ActivePlanModeButton
      enabled={Boolean(expert)}
      disabled={Boolean(inputState.disabled)}
      label={expert ? resolveExpertLabel(expert) : "专家"}
      showIcon={false}
      onDisable={onDisable}
    />
  );
}

const addPlanModeScopeAlias = (
  state: PlanModeLocalState,
  alias: string | null | undefined,
): PlanModeLocalState => {
  if (!alias || alias === state.scopeKey || state.aliases?.includes(alias)) {
    return state;
  }

  return {
    ...state,
    aliases: [...(state.aliases || []), alias],
  };
};

export default function ChatPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { isDark } = useTheme();
  const showContentOnly = useChatPresentationStore(
    (state) => state.showContentOnly,
  );
  // ==================== 品牌主题 (Kun He) ====================
  // 获取动态品牌配置，用于 welcome avatar
  const { theme: brandTheme } = useBrandTheme();
  // ==================== 品牌主题结束 ====================
  const isContentOnly = showContentOnly;
  const chatId = useMemo(() => {
    const match = location.pathname.match(/^\/chat\/(.+)$/);
    return match?.[1];
  }, [location.pathname]);
  const [showModelPrompt, setShowModelPrompt] = useState(false);
  const [jobs, setJobs] = useState<CronJobSpecOutput[]>([]);
  const [taskProgress, setTaskProgress] = useState<ChatTaskProgressData | null>(
    null,
  );
  const [subAgentMonitorResetKey, setSubAgentMonitorResetKey] = useState(0);
  const { selectedAgent } = useAgentStore();
  const [selectedExpertId, setSelectedExpertId] = useState<string | null>(null);
  const [experts, setExperts] = useState<SelectableExpert[]>([]);
  const [expertsLoading, setExpertsLoading] = useState(true);
  const [modelRefreshKey, setModelRefreshKey] = useState(0);
  const [feedbackRefreshKey, setFeedbackRefreshKey] = useState(0);
  const [autoPreviewTriggerKey, setAutoPreviewTriggerKey] = useState(0);
  const [isDragging, setIsDragging] = useState(false);
  const [wPlusSopLocksChatInput, setWPlusSopLocksChatInput] = useState(false);
  const [selectedContextReferences, setSelectedContextReferences] = useState<
    SkillMentionItem[]
  >([]);
  const [contextReferences, setContextReferences] = useState<
    SkillMentionItem[]
  >([]);
  const [contextReferencesLoading, setContextReferencesLoading] =
    useState(false);
  const [contextReferencesError, setContextReferencesError] = useState(false);
  const pendingContextReferencesRef = useRef<SkillMentionItem[]>([]);
  const pendingScenarioPresetIdRef = useRef<string | null>(null);
  const contextReferencesRequestIdRef = useRef(0);
  const dragCounterRef = useRef(0);
  const runtimeLoadingBridgeRef = useRef<RuntimeLoadingBridgeApi | null>(null);
  const { message } = useAppMessage();
  const messageRef = useRef(message);
  messageRef.current = message;
  const composerInputState = useChatAnywhereInput((value) => ({
    disabled: Boolean(value.disabled),
    loading: Boolean(value.loading),
  }));
  const composerDisabled = Boolean(composerInputState.disabled);
  const composerLoading = Boolean(composerInputState.loading);
  const [taskEditForm] = Form.useForm<CronJobSpecOutput>();
  const [editingTask, setEditingTask] = useState<CronJobSpecOutput | null>(
    null,
  );
  const [taskEditSaving, setTaskEditSaving] = useState(false);
  const {
    sessions,
    setSessions,
    setSessionLoading,
    currentSessionId: activeSessionId,
  } = useChatAnywhereSessionsState();
  const contextUsageChatId = chatId
    ? sessionApi.getChatIdForSession(chatId)
    : activeSessionId
    ? sessionApi.getChatIdForSession(activeSessionId)
    : null;
  const contextUsage = useContextUsageController(
    contextUsageChatId,
    composerLoading,
  );

  useEffect(() => {
    setSelectedContextReferences([]);
    pendingContextReferencesRef.current = [];
  }, [activeSessionId, chatId]);

  useEffect(() => {
    setSelectedExpertId(null);
  }, [selectedAgent]);

  useEffect(() => {
    let cancelled = false;

    const loadExperts = async () => {
      setExpertsLoading(true);
      try {
        const records = await expertsApi.listExperts();
        if (!cancelled) {
          setExperts(normalizeSelectableExperts(records as Expert[]));
        }
      } catch (error) {
        if (!cancelled) {
          setExperts([]);
          messageRef.current.error(
            error instanceof Error ? error.message : "加载专家失败",
          );
        }
      } finally {
        if (!cancelled) {
          setExpertsLoading(false);
        }
      }
    };

    void loadExperts();
    return () => {
      cancelled = true;
    };
  }, []);
  const sourceSystemConfig = useSourceSystemConfigStore(
    (state) => state.config,
  );
  const loadActiveModelData = useProviderModelStore(
    (state) => state.loadActiveModelData,
  );
  const taskProgressEnabled = isChatTaskProgressEnabled(sourceSystemConfig);

  const loadContextReferences = useCallback((query: string) => {
    const requestId = ++contextReferencesRequestIdRef.current;
    setContextReferencesLoading(true);
    setContextReferencesError(false);
    void contextReferencesApi
      .discover(query)
      .then((response) => {
        if (requestId !== contextReferencesRequestIdRef.current) return;
        setContextReferences(
          selectContextReferences(
            [...response.skills, ...response.mcp_tools, ...response.files],
            query,
          ),
        );
      })
      .catch(() => {
        if (requestId !== contextReferencesRequestIdRef.current) return;
        setContextReferences([]);
        setContextReferencesError(true);
      })
      .finally(() => {
        if (requestId === contextReferencesRequestIdRef.current)
          setContextReferencesLoading(false);
      });
  }, []);

  // useTransition for non-urgent state updates (badge clearing)
  const [, startTransition] = useTransition();
  // Debounce flag for markTaskRead API calls
  const markTaskReadPendingRef = useRef(false);

  const isChatActiveRef = useRef(false);
  isChatActiveRef.current =
    location.pathname === "/" || location.pathname.startsWith("/chat");

  useEffect(() => {
    const handler = () => {
      message.error(t("chat.followUp.autoSubmitFailed"));
    };

    document.addEventListener(FOLLOW_UP_SUBMIT_FAILED_EVENT, handler);
    return () =>
      document.removeEventListener(FOLLOW_UP_SUBMIT_FAILED_EVENT, handler);
  }, [message, t]);

  useEffect(() => {
    const handler = (event: Event) => {
      if (!taskProgressEnabled) {
        setTaskProgress(null);
        return;
      }
      const update = normalizeTaskProgressUpdateEventDetail(
        (event as CustomEvent<ChatTaskProgressUpdateDetail>).detail,
      );
      if (
        !isTaskProgressUpdateForActiveSession(update, [
          chatId,
          activeSessionId,
          window.currentSessionId,
          chatId ? sessionApi.getChatIdForSession(chatId) : null,
          activeSessionId
            ? sessionApi.getChatIdForSession(activeSessionId)
            : null,
          chatId ? sessionApi.getLogicalSessionId(chatId) : null,
          activeSessionId
            ? sessionApi.getLogicalSessionId(activeSessionId)
            : null,
        ])
      ) {
        return;
      }

      const detail = update.task_progress;
      if (!detail) {
        setTaskProgress(null);
        return;
      }
      setTaskProgress((previous) => {
        if (
          previous &&
          previous.turn_id === detail.turn_id &&
          previous.version > detail.version
        ) {
          return previous;
        }
        return detail;
      });
    };

    document.addEventListener(CHAT_TASK_PROGRESS_UPDATE_EVENT, handler);
    return () =>
      document.removeEventListener(CHAT_TASK_PROGRESS_UPDATE_EVENT, handler);
  }, [activeSessionId, chatId, taskProgressEnabled]);

  useEffect(() => {
    if (!taskProgressEnabled) {
      setTaskProgress(null);
    }
  }, [taskProgressEnabled]);

  const isChatActive = useCallback(() => isChatActiveRef.current, []);

  // Use custom hooks for better separation of concerns
  const isComposingRef = useIMEComposition(isChatActive);
  const multimodalCaps = useMultimodalCapabilities(
    modelRefreshKey,
    location.pathname,
    isChatActive,
  );

  const lastSessionIdRef = useRef<string | null>(null);
  /** Tracks the stale auto-selected session ID that was skipped on init, so we can suppress its late-arriving onSessionSelected callback. */
  const staleAutoSelectedIdRef = useRef<string | null>(null);
  const taskHadResultRef = useRef(false);
  const previousCurrentTaskRef = useRef<CronJobSpecOutput | null>(null);
  const chatIdRef = useRef(chatId);
  const navigateRef = useRef(navigate);
  const chatRef = useRef<IAgentScopeRuntimeWebUIRef>(null);
  chatIdRef.current = chatId;
  navigateRef.current = navigate;

  // Tell sessionApi which session to put first in getSessionList, so the library's
  // useMount auto-selects the correct session without an extra getSession round-trip.
  if (chatId && sessionApi.preferredChatId !== chatId) {
    sessionApi.preferredChatId = chatId;
  }

  // Register session API event callbacks for URL synchronization

  useEffect(() => {
    sessionApi.onSessionIdResolved = (tempId, realId) => {
      if (!isChatActiveRef.current) return;
      if (pendingPlanModePersistScopesRef.current.delete(tempId)) {
        pendingPlanModePersistScopesRef.current.add(realId);
        resolvedPlanModePersistScopesRef.current.set(tempId, realId);
      }
      setPlanModeLocalState((current) => {
        const isResolvedPlanModeScope =
          current.scopeKey === tempId || current.aliases?.includes(tempId);

        if (!current.enabled && !isResolvedPlanModeScope) {
          return current;
        }

        return addPlanModeScopeAlias(current, realId);
      });
      // Update URL when realId is resolved, regardless of current chatId
      // (chatId may be undefined if URL was cleared in onSessionCreated)
      lastSessionIdRef.current = realId;
      navigateRef.current(`/chat/${realId}`, { replace: true });
    };

    sessionApi.onSessionRemoved = (removedId) => {
      if (!isChatActiveRef.current) return;
      // Clear URL when current session is removed
      // Check if removed session matches current session (by realId or sessionId)
      const currentRealId = sessionApi.getRealIdForSession(
        chatIdRef.current || "",
      );
      if (chatIdRef.current === removedId || currentRealId === removedId) {
        lastSessionIdRef.current = null;
        navigateRef.current("/chat", { replace: true });
      }
    };

    sessionApi.onSessionSelected = (
      sessionId: string | null | undefined,
      realId: string | null,
    ) => {
      if (!isChatActiveRef.current) return;
      // Update URL when session is selected and different from current
      const targetId = realId || sessionId;
      if (!targetId) return;

      // If current URL's chatId differs from targetId, skip this callback.
      // This happens when user quickly switches sessions via sidebar:
      // 1. User clicks A → getSession(A) starts
      // 2. User clicks B → URL becomes /chat/B
      // 3. A's request completes → onSessionSelected(A) fires
      // 4. Should NOT navigate back to A since user already chose B
      const currentUrlChatId = chatIdRef.current;
      if (
        currentUrlChatId &&
        currentUrlChatId !== targetId &&
        !matchesResolvedChatId({
          requestedSessionId: currentUrlChatId,
          chatId: targetId,
        })
      ) {
        return;
      }

      // If a preferred chatId from the URL exists and no navigation has happened yet,
      // skip the library's initial auto-selection (always first session).
      // ChatSessionInitializer will apply the correct selection afterward.
      if (
        chatIdRef.current &&
        lastSessionIdRef.current === null &&
        targetId !== chatIdRef.current
      ) {
        lastSessionIdRef.current = targetId;
        // Record the stale ID so its delayed getSession callback is also suppressed.
        staleAutoSelectedIdRef.current = targetId;
        return;
      }

      // Suppress the stale getSession callback that arrives after the correct session loads.
      if (
        staleAutoSelectedIdRef.current &&
        staleAutoSelectedIdRef.current === targetId
      ) {
        staleAutoSelectedIdRef.current = null;
        return;
      }

      if (targetId !== lastSessionIdRef.current) {
        lastSessionIdRef.current = targetId;
        navigateRef.current(`/chat/${targetId}`, { replace: true });
      }
    };

    sessionApi.onSessionCreated = (sessionId) => {
      if (!isChatActiveRef.current) return;
      setPlanModeLocalState((current) =>
        current.enabled ||
        pendingPlanModePersistScopesRef.current.has(current.scopeKey)
          ? addPlanModeScopeAlias(current, sessionId)
          : current,
      );
      // Clear URL when creating new session, wait for realId resolution to update
      lastSessionIdRef.current = null;
      navigateRef.current("/chat", { replace: true });
    };

    return () => {
      sessionApi.onSessionIdResolved = null;
      sessionApi.onSessionRemoved = null;
      sessionApi.onSessionSelected = null;
      sessionApi.onSessionCreated = null;
    };
  }, []);

  useEffect(() => {
    setTaskProgress(null);
  }, [chatId, location.pathname]);

  // ==================== URL 导航参数 (Kun He, 2026-04-15) ====================
  // 处理 iframe URL 传递的 sessionId/taskId 参数，自动跳转到对应聊天页面
  // sessionId: 可传 backend chat.id 或逻辑 session_id，后续由初始选择逻辑解析
  // taskId: 查找 task.chat_id 后导航
  const sessionIdRef = useRef<string | null>(null);
  const taskIdRef = useRef<string | null>(null);

  useEffect(() => {
    const store = useIframeStore.getState();
    const { sessionId, taskId } = store;

    // 只在首次加载时处理，避免重复导航
    if (sessionId) {
      sessionIdRef.current = sessionId;
      taskIdRef.current = null; // sessionId 优先，忽略 taskId
      store.clearNavigationParams();
      console.info("[Chat] Navigating to sessionId:", sessionId);
      navigate(`/chat/${sessionId}`, { replace: true });
      return;
    }

    if (taskId) {
      taskIdRef.current = taskId;
      store.clearNavigationParams();
      console.info("[Chat] taskId set, waiting for jobs:", taskId);
    }
  }, [navigate]);

  // taskId 导航需要等待 jobs 加载完成
  useEffect(() => {
    if (!taskIdRef.current || jobs.length === 0) return;

    const task = jobs.find((j) => j.id === taskIdRef.current);
    const chatId = task?.task?.chat_id;

    if (chatId) {
      setAutoPreviewTriggerKey((prev) => prev + 1);
      navigate(`/chat/${chatId}`, { replace: true });
      taskIdRef.current = null;
    } else {
      taskIdRef.current = null;
    }
  }, [jobs, navigate]);
  // ==================== URL 导航参数结束 ====================

  // Setup multimodal capabilities tracking via custom hook

  // Refresh chat when selectedAgent changes
  const prevSelectedAgentRef = useRef(selectedAgent);
  useEffect(() => {
    // Only refresh if selectedAgent actually changed (not initial mount)
    if (
      prevSelectedAgentRef.current !== selectedAgent &&
      prevSelectedAgentRef.current !== undefined
    ) {
      setModelRefreshKey((prev) => prev + 1);
    }
    prevSelectedAgentRef.current = selectedAgent;
  }, [selectedAgent]);

  const refreshJobs = useCallback(async () => {
    try {
      const nextJobs = await cronJobApi.listCronJobs();
      setJobs(Array.isArray(nextJobs) ? nextJobs : []);
    } catch {
      setJobs([]);
    }
  }, []);

  const { tasks, currentTask } = useMemo(
    () => deriveChatTaskState(jobs, chatId),
    [jobs, chatId],
  );
  const feedbackTask = useMemo(
    () =>
      currentTask
        ? {
            cronTaskId: currentTask.id,
            cronTaskName: currentTask.name || currentTask.id,
          }
        : null,
    [currentTask],
  );
  const [feedbackItems, setFeedbackItems] = useState<FeedbackRecord[]>([]);
  const [feedbackLoading, setFeedbackLoading] = useState(false);
  const feedbackUserId = useIframeStore((state) => state.userId);
  const isOriginY = useIframeStore((state) => state.isOriginY);
  const voiceRecorderEnabled = shouldShowGlobalVoiceRecorder(
    feedbackUserId,
    showContentOnly,
    isOriginY,
  );
  const skipPreviewTracking = useIframeStore(
    (state) => state.skipPreviewTracking,
  );
  const feedbackAllowed = useMemo(
    () => isResponseFeedbackUserAllowed(feedbackUserId),
    [feedbackUserId],
  );
  const feedbackChatId = useMemo(() => {
    const routeChatId = chatId ? sessionApi.getChatIdForSession(chatId) : null;
    if (routeChatId) {
      return routeChatId;
    }

    const fallbackSessionId = activeSessionId || window.currentSessionId || "";
    return sessionApi.getChatIdForSession(fallbackSessionId);
  }, [chatId, activeSessionId]);
  const feedbackSessionId = useMemo(() => {
    const activeSession = sessions.find(
      (session) =>
        session.id === activeSessionId ||
        session.id === chatId ||
        (session as { sessionId?: string }).sessionId === activeSessionId ||
        (session as { sessionId?: string }).sessionId === chatId,
    ) as unknown as { sessionId?: string; session_id?: string } | undefined;

    return (
      activeSession?.sessionId ||
      activeSession?.session_id ||
      sessionApi.getLogicalSessionId(activeSessionId || "") ||
      window.currentSessionId ||
      chatId ||
      null
    );
  }, [activeSessionId, chatId, sessions]);
  const activeFeedbackResponses = useMemo(() => {
    const activeSession = sessions.find(
      (session) =>
        session.id === activeSessionId ||
        session.id === chatId ||
        (session as { sessionId?: string }).sessionId === activeSessionId ||
        (session as { sessionId?: string }).sessionId === chatId,
    );
    return collectFeedbackResponsesFromMessages(activeSession?.messages || []);
  }, [activeSessionId, chatId, sessions]);
  const feedbackLookup = useMemo<FeedbackLookupMap>(
    () => buildFeedbackLookup(feedbackItems, activeFeedbackResponses),
    [activeFeedbackResponses, feedbackItems],
  );
  const hasRunningTask = useMemo(
    () => tasks.some((task) => task.task?.is_running),
    [tasks],
  );
  const lastFeedbackSessionIdRef = useRef<string | null>(null);
  const feedbackLookupPending = Boolean(
    feedbackAllowed &&
      feedbackSessionId &&
      (feedbackLoading ||
        feedbackSessionId !== lastFeedbackSessionIdRef.current),
  );
  const activePlanModeSessionIds = useMemo(
    () => (chatId ? [chatId] : [activeSessionId]),
    [activeSessionId, chatId],
  );
  const activePlanModeSession = useMemo<PlanModeSession | null>(() => {
    return resolveActivePlanModeSession(
      sessions,
      activePlanModeSessionIds,
    ) as PlanModeSession | null;
  }, [activePlanModeSessionIds, sessions]);
  const activePlanModeMetadataEnabled = getPlanModeEnabled(
    activePlanModeSession,
  );
  const activePlanModeScopeKey = chatId || activeSessionId || "";
  const [planModeLocalState, setPlanModeLocalState] =
    useState<PlanModeLocalState>({
      scopeKey: activePlanModeScopeKey,
      enabled: activePlanModeMetadataEnabled,
    });
  const activePlanModeSessionRef = useRef<PlanModeSession | null>(null);
  const activePlanModeScopeKeyRef = useRef(activePlanModeScopeKey);
  const pendingPlanModePersistScopesRef = useRef(new Set<string>());
  const resolvedPlanModePersistScopesRef = useRef(new Map<string, string>());
  const planModeLocalStateForActiveScope =
    planModeLocalState.scopeKey === "" &&
    activePlanModeScopeKey &&
    planModeLocalState.enabled &&
    pendingPlanModePersistScopesRef.current.has("")
      ? addPlanModeScopeAlias(planModeLocalState, activePlanModeScopeKey)
      : planModeLocalState;
  const planModeEnabled = getScopedPlanModeEnabled({
    metadataEnabled: activePlanModeMetadataEnabled,
    localState: planModeLocalStateForActiveScope,
    scopeKey: activePlanModeScopeKey,
  });
  const [pendingPlanRevision, setPendingPlanRevision] =
    useState<PendingPlanRevision | null>(null);
  const [goalModeEnabled, setGoalModeEnabled] = useState(false);
  activePlanModeSessionRef.current = activePlanModeSession;
  activePlanModeScopeKeyRef.current = activePlanModeScopeKey;

  useEffect(() => {
    setPlanModeLocalState((current) => {
      if (
        current.scopeKey === activePlanModeScopeKey &&
        pendingPlanModePersistScopesRef.current.has(activePlanModeScopeKey)
      ) {
        return current;
      }
      if (
        current.enabled &&
        current.aliases?.includes(activePlanModeScopeKey)
      ) {
        return {
          ...current,
          scopeKey: activePlanModeScopeKey,
        };
      }
      if (
        current.scopeKey === "" &&
        activePlanModeScopeKey &&
        pendingPlanModePersistScopesRef.current.has(current.scopeKey)
      ) {
        pendingPlanModePersistScopesRef.current.delete(current.scopeKey);
        pendingPlanModePersistScopesRef.current.add(activePlanModeScopeKey);
        resolvedPlanModePersistScopesRef.current.set(
          current.scopeKey,
          activePlanModeScopeKey,
        );
        return {
          scopeKey: activePlanModeScopeKey,
          enabled: current.enabled,
          aliases: current.aliases,
        };
      }
      return {
        scopeKey: activePlanModeScopeKey,
        enabled: activePlanModeMetadataEnabled,
      };
    });
  }, [activePlanModeMetadataEnabled, activePlanModeScopeKey]);

  const setPlanModeEnabledForScope = useCallback(
    (scopeKey: string, enabled: boolean) => {
      setPlanModeLocalState((current) => {
        const resolvedScopeKey =
          resolvedPlanModePersistScopesRef.current.get(scopeKey) || scopeKey;
        if (activePlanModeScopeKeyRef.current !== resolvedScopeKey) {
          return current;
        }
        return { scopeKey: resolvedScopeKey, enabled };
      });
    },
    [],
  );

  const setPlanModeEnabledForActiveScope = useCallback(
    (enabled: boolean) => {
      setPlanModeEnabledForScope(activePlanModeScopeKeyRef.current, enabled);
    },
    [setPlanModeEnabledForScope],
  );

  const activePlanRevisionScopeKey =
    activePlanModeSession?.id ||
    chatId ||
    activeSessionId ||
    window.currentSessionId ||
    "";

  useEffect(() => {
    setPendingPlanRevision(null);
  }, [activePlanRevisionScopeKey]);

  const ensurePlanModeChatId = useCallback(
    async (
      session: PlanModeSession | null,
      meta: Record<string, unknown>,
    ): Promise<string | null> => {
      const candidateSessionId =
        chatId ||
        session?.id ||
        activeSessionId ||
        window.currentSessionId ||
        "";
      const existingChatId =
        (chatId ? sessionApi.getChatIdForSession(chatId) : null) ||
        (chatId && !/^\d+$/.test(chatId) ? chatId : null) ||
        sessionApi.getChatIdForSession(candidateSessionId) ||
        session?.realId ||
        (session?.id && !/^\d+$/.test(session.id) ? session.id : null);

      if (existingChatId) {
        return existingChatId;
      }

      const logicalSessionId =
        session?.sessionId ||
        session?.session_id ||
        sessionApi.getLogicalSessionId(candidateSessionId) ||
        candidateSessionId ||
        `${getChannel()}:${getUserId()}`;
      const created = await chatApi.createChat({
        session_id: logicalSessionId,
        user_id: getUserId(session?.userId),
        channel: getChannel(session?.channel),
        name: session?.name || "新会话",
        meta,
      });
      await sessionApi.getSessionList();
      return created.id;
    },
    [activeSessionId, chatId],
  );

  const persistPlanMode = useCallback(
    async (enabled: boolean) => {
      const previousSelectedExpertId = selectedExpertId;
      if (enabled) {
        setSelectedExpertId(null);
      }
      const scopeKey = activePlanModeScopeKeyRef.current;
      const retainBlankScope =
        enabled && scopeKey === "" && !activePlanModeMetadataEnabled;
      pendingPlanModePersistScopesRef.current.add(scopeKey);
      let persistSucceeded = false;
      try {
        await persistPlanModeState({
          enabled,
          session: activePlanModeSessionRef.current,
          ensureChatId: ensurePlanModeChatId,
          updateChat: chatApi.updateChat,
          updateSession: async (session) => {
            const nextSessions = await sessionApi.updateSession(
              session as Parameters<typeof sessionApi.updateSession>[0] & {
                meta: Record<string, unknown>;
              },
              { refreshList: false },
            );
            setSessions(nextSessions);
          },
          setPlanModeEnabled: (nextEnabled) => {
            setPlanModeEnabledForScope(scopeKey, nextEnabled);
          },
          onPersistError: () => {
            message.error(
              t("chat.planMode.persistFailed", "Plan Mode 保存失败"),
            );
          },
        });
        persistSucceeded = true;
      } catch (error) {
        if (enabled) {
          setSelectedExpertId(previousSelectedExpertId);
        }
        throw error;
      } finally {
        const resolvedScopeKey =
          resolvedPlanModePersistScopesRef.current.get(scopeKey);
        if (!(persistSucceeded && retainBlankScope)) {
          pendingPlanModePersistScopesRef.current.delete(scopeKey);
        }
        if (resolvedScopeKey) {
          pendingPlanModePersistScopesRef.current.delete(resolvedScopeKey);
          resolvedPlanModePersistScopesRef.current.delete(scopeKey);
        }
      }
    },
    [
      activePlanModeMetadataEnabled,
      ensurePlanModeChatId,
      message,
      selectedExpertId,
      setPlanModeEnabledForScope,
      setSelectedExpertId,
      setSessions,
      t,
    ],
  );

  const handleContinueModifyingPlan = useCallback(
    (data: ChatPlanReviewCardData) => {
      setPendingPlanRevision({
        planId: data.plan_id,
      });
      setPlanModeEnabledForActiveScope(true);
      if (!planModeEnabled) {
        void persistPlanMode(true);
      }
    },
    [persistPlanMode, planModeEnabled, setPlanModeEnabledForActiveScope],
  );

  const handlePlanModeDecision = useCallback(
    (enabled: boolean) => {
      setPendingPlanRevision(null);
      setPlanModeEnabledForActiveScope(enabled);
      void persistPlanMode(enabled);
    },
    [persistPlanMode, setPlanModeEnabledForActiveScope],
  );

  const activePlanModeControl = useMemo(
    () => (
      <ActivePlanModeControl
        enabled={planModeEnabled}
        label={t("chat.planMode.label", "计划模式")}
        displayLabel={t("chat.planMode.shortLabel", "计划")}
        onDisable={() => {
          setPendingPlanRevision(null);
          void persistPlanMode(false);
        }}
      />
    ),
    [persistPlanMode, planModeEnabled, t],
  );

  const activeGoalModeControl = useMemo(
    () => (
      <ActiveGoalModeControl
        enabled={goalModeEnabled}
        onDisable={() => setGoalModeEnabled(false)}
      />
    ),
    [goalModeEnabled],
  );

  const selectedExpert = useMemo(
    () => experts.find((expert) => expert.id === selectedExpertId) || null,
    [experts, selectedExpertId],
  );

  const activeExpertControl = useMemo(
    () => (
      <ActiveExpertControl
        expert={selectedExpert}
        onDisable={() => setSelectedExpertId(null)}
      />
    ),
    [selectedExpert],
  );

  useEffect(() => {
    if (!feedbackAllowed) {
      setFeedbackItems([]);
      setFeedbackLoading(false);
      lastFeedbackSessionIdRef.current = null;
      return;
    }

    const sessionId = feedbackSessionId;
    if (!sessionId) {
      setFeedbackLoading(false);
      return;
    }

    const sessionChanged = sessionId !== lastFeedbackSessionIdRef.current;

    // 会话确实切换时清空旧数据，避免显示上一个会话的反馈
    if (sessionChanged) {
      setFeedbackItems([]);
    }
    lastFeedbackSessionIdRef.current = sessionId;

    let cancelled = false;
    setFeedbackLoading(sessionChanged);
    feedbackApi
      .getSessionFeedbacks({
        chatId: feedbackChatId,
        sessionId,
      })
      .then((result) => {
        if (cancelled) return;
        setFeedbackItems(result.items || []);
        setFeedbackLoading(false);
      })
      .catch(() => {
        if (!cancelled) {
          if (sessionChanged) {
            setFeedbackItems([]);
          }
          setFeedbackLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [feedbackAllowed, feedbackChatId, feedbackSessionId, feedbackRefreshKey]);

  const handleFeedbackSaved = useCallback((feedback: FeedbackRecord) => {
    setFeedbackItems((prev) => [
      feedback,
      ...prev.filter((item) => item.id !== feedback.id),
    ]);
  }, []);

  useEffect(() => {
    void refreshJobs();

    // 仅从其他标签页切换回来时刷新（移除 window.focus 触发，减少不必要的 API 调用）
    const handleVisibilityRefresh = () => {
      if (document.visibilityState === "visible") {
        void refreshJobs();
      }
    };

    // 监听定时任务创建成功事件
    const handleTaskCreated = () => {
      void refreshJobs();
    };

    document.addEventListener("visibilitychange", handleVisibilityRefresh);
    document.addEventListener("taskCreated", handleTaskCreated);

    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityRefresh);
      document.removeEventListener("taskCreated", handleTaskCreated);
    };
  }, [refreshJobs]);

  useEffect(() => {
    const pollMs = hasRunningTask
      ? TASK_RUNNING_POLL_MS
      : currentTask?.task?.has_scheduled_result === false
      ? TASK_PENDING_POLL_MS
      : TASK_PAGE_POLL_MS;

    const intervalId = window.setInterval(() => {
      void refreshJobs();
    }, pollMs);

    return () => window.clearInterval(intervalId);
  }, [currentTask?.task?.has_scheduled_result, hasRunningTask, refreshJobs]);

  useEffect(() => {
    const hadResult = Boolean(currentTask?.task?.has_scheduled_result);
    if (hadResult && !taskHadResultRef.current) {
      void chatRef.current?.refreshSession?.();
      setFeedbackRefreshKey((prev) => prev + 1);
    }
    taskHadResultRef.current = hadResult;
  }, [currentTask?.task?.has_scheduled_result]);

  useEffect(() => {
    if (!currentTask?.id) return;
    if ((currentTask.task?.unread_execution_count || 0) <= 0) return;
    if (!shouldMarkTaskReadOnOpen(currentTask)) return;

    // Debounce: skip if there's already a pending markTaskRead request
    if (markTaskReadPendingRef.current) return;

    markTaskReadPendingRef.current = true;

    // Non-urgent update: badge clearing can be delayed
    startTransition(() => {
      setJobs((prev) =>
        prev.map((job) =>
          job.id === currentTask.id && job.task
            ? {
                ...job,
                task: {
                  ...job.task,
                  unread_execution_count: 0,
                },
              }
            : job,
        ),
      );
    });

    void cronJobApi
      .markTaskRead(currentTask.id, false)
      .catch(() => {})
      .finally(() => {
        markTaskReadPendingRef.current = false;
      });
  }, [currentTask?.id, currentTask?.task?.unread_execution_count]);

  const handleTaskOpen = useCallback(
    (task: CronJobSpecOutput) => {
      const taskOpenTarget = getTaskOpenTarget(task);
      if (!taskOpenTarget) return;
      const shouldAutoPreviewOnOpen = taskOpenTarget !== chatIdRef.current;

      // Force loading to render immediately before navigate triggers re-render
      flushSync(() => {
        setSessionLoading(true);
      });

      if (shouldAutoPreviewOnOpen) {
        setAutoPreviewTriggerKey((prev) => prev + 1);
      }
      navigate(`/chat/${taskOpenTarget}`, { replace: true });
    },
    [navigate, setSessionLoading],
  );

  const handleTaskResume = useCallback(
    async (task: CronJobSpecOutput) => {
      setJobs((prev) =>
        prev.map((job) =>
          job.id === task.id
            ? {
                ...job,
                enabled: true,
                task: job.task
                  ? {
                      ...job.task,
                      is_paused: false,
                      pause_reason: null,
                      auto_paused_at: null,
                      unread_execution_count: 0,
                    }
                  : job.task,
              }
            : job,
        ),
      );

      try {
        await cronJobApi.resumeCronJob(task.id);
        message.success("任务已恢复");
        void refreshJobs();
      } catch {
        message.error("恢复失败");
        void refreshJobs();
      }
    },
    [message, refreshJobs],
  );

  const handleTaskPause = useCallback(
    async (task: CronJobSpecOutput) => {
      setJobs((prev) =>
        prev.map((job) =>
          job.id === task.id
            ? {
                ...job,
                enabled: false,
                task: job.task
                  ? {
                      ...job.task,
                      is_paused: true,
                      pause_reason: "manual",
                    }
                  : job.task,
              }
            : job,
        ),
      );

      try {
        await cronJobApi.pauseCronJob(task.id);
        message.success("任务已停止");
        void refreshJobs();
      } catch {
        message.error("停止失败");
        void refreshJobs();
      }
    },
    [message, refreshJobs],
  );

  const handleTaskRun = useCallback(
    async (task: CronJobSpecOutput) => {
      setJobs((prev) =>
        prev.map((job) =>
          job.id === task.id
            ? {
                ...job,
                state: {
                  ...job.state,
                  last_status: "running",
                  last_error: null,
                },
                task: job.task
                  ? {
                      ...job.task,
                      is_running: true,
                    }
                  : job.task,
              }
            : job,
        ),
      );

      try {
        await cronJobApi.runCronJob(task.id);
        message.success("任务已开始执行");
        void refreshJobs();
      } catch {
        message.error("执行失败");
        void refreshJobs();
      }
    },
    [message, refreshJobs],
  );

  const handleTaskDelete = useCallback(
    (task: CronJobSpecOutput) => {
      Modal.confirm({
        title: "删除任务",
        content: `确认删除任务“${task.name || task.id}”？删除后无法恢复。`,
        centered: true,
        okText: "删除",
        okType: "danger",
        cancelText: "取消",
        cancelButtonProps: { type: "text" },
        onOk: async () => {
          setJobs((prev) => prev.filter((job) => job.id !== task.id));
          if (task.task?.chat_id && task.task.chat_id === chatIdRef.current) {
            navigate("/chat", { replace: true });
          }
          try {
            await cronJobApi.deleteCronJob(task.id);
            message.success("任务已删除");
            void refreshJobs();
          } catch {
            message.error("删除失败");
            void refreshJobs();
          }
        },
      });
    },
    [message, navigate, refreshJobs],
  );

  const handleTaskEdit = useCallback(
    (task: CronJobSpecOutput) => {
      const formValues = buildCronJobFormValues(task);
      setEditingTask(task);
      taskEditForm.setFieldsValue({
        ...formValues,
        taskContentText:
          task.task_type === "text"
            ? formValues.text || ""
            : extractTaskContentText(formValues.request?.input),
      } as Parameters<typeof taskEditForm.setFieldsValue>[0]);
    },
    [taskEditForm],
  );

  const handleTaskEditClose = useCallback(() => {
    if (taskEditSaving) return;
    setEditingTask(null);
    taskEditForm.resetFields();
  }, [taskEditForm, taskEditSaving]);

  const handleTaskEditSubmit = useCallback(
    async (values: CronTaskEditFormValues) => {
      if (!editingTask) return;

      setTaskEditSaving(true);
      try {
        await submitCronTaskEdit(
          editingTask,
          values,
          cronJobApi.replaceCronJob,
        );
        message.success("任务已更新");
        setEditingTask(null);
        taskEditForm.resetFields();
        void refreshJobs();
      } catch (error) {
        console.error("Failed to update cron task from chat sidebar:", error);
        message.error(
          error instanceof SyntaxError ? "任务配置格式不正确" : "保存失败",
        );
      } finally {
        setTaskEditSaving(false);
      }
    },
    [editingTask, message, refreshJobs, taskEditForm],
  );

  useEffect(() => {
    const previousTask = previousCurrentTaskRef.current;
    previousCurrentTaskRef.current = currentTask;

    if (
      !shouldRefreshCurrentTaskMessages({
        previousTask,
        currentTask,
      })
    ) {
      return;
    }

    void chatRef.current?.refreshSession?.();
  }, [
    currentTask?.id,
    currentTask?.task?.has_scheduled_result,
    currentTask?.task?.last_scheduled_run_at,
    currentTask?.task?.unread_execution_count,
  ]);

  // Show toast when task has no scheduled result yet
  const taskNoResultShownIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (currentTask && !currentTask.task?.has_scheduled_result) {
      if (taskNoResultShownIdRef.current !== currentTask.id) {
        taskNoResultShownIdRef.current = currentTask.id;
        message.info("当前任务暂未启动，等下次收到提醒再来看看哟~");
      }
    } else {
      taskNoResultShownIdRef.current = null;
    }
  }, [currentTask?.id, currentTask?.task?.has_scheduled_result]);

  const copyResponse = useCallback(
    async (response: CopyableResponse) => {
      try {
        await copyText(extractCopyableText(response));
        message.success(t("common.copied"));
      } catch {
        message.error(t("common.copyFailed"));
      }
    },
    [t],
  );

  const resolveLogicalRequestSessionId = useCallback(
    (target: ChatRequestTarget, session?: SessionInfo): string => {
      if (target.logical_session_id) {
        return target.logical_session_id;
      }

      return sessionApi.getLogicalSessionId(
        target.session_id ||
          window.currentSessionId ||
          session?.session_id ||
          "",
      );
    },
    [],
  );

  const resolveRequestChatId = useCallback(
    (target: ChatRequestTarget, logicalSessionId: string): string => {
      return (
        target.chat_id ||
        sessionApi.getChatIdForSession(logicalSessionId) ||
        sessionApi.getChatIdForSession(target.session_id || "") ||
        target.session_id ||
        chatIdRef.current ||
        logicalSessionId
      );
    },
    [],
  );

  const customFetch = useCallback(
    async (data: {
      input?: Array<Record<string, unknown>>;
      biz_params?: Record<string, unknown>;
      signal?: AbortSignal;
      session_id?: string;
      logical_session_id?: string;
      chat_id?: string | null;
    }): Promise<Response> => {
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        ...buildAuthHeaders(),
      };

      try {
        const activeModels = await loadActiveModelData({
          scope: "effective",
        });
        if (
          !activeModels?.active_llm?.provider_id ||
          !activeModels?.active_llm?.model
        ) {
          setShowModelPrompt(true);
          return buildModelError();
        }
      } catch {
        setShowModelPrompt(true);
        return buildModelError();
      }

      const {
        input = [],
        biz_params,
        session_id,
        logical_session_id,
        chat_id,
      } = data;
      const session: SessionInfo = input[input.length - 1]?.session || {};
      const lastInput = input.slice(-1);
      const lastMsg = lastInput[0];
      const rewrittenInput =
        lastMsg?.content && Array.isArray(lastMsg.content)
          ? [
              {
                ...lastMsg,
                content: lastMsg.content.map(normalizeContentUrls),
              },
            ]
          : lastInput;
      const userText = rewrittenInput
        .filter((m: InputMessage) => m.role === "user")
        .map(extractUserMessageText)
        .join("\n")
        .trim();

      const resolvedLogicalSessionId = resolveLogicalRequestSessionId(
        {
          session_id,
          logical_session_id,
          chat_id,
        },
        session,
      );

      const requestBody = {
        input: rewrittenInput,
        session_id: resolvedLogicalSessionId,
        // ==================== userId 统一整改 (Kun He) ====================
        // 使用 getUserId()/getChannel() 获取，优先级：iframe > window > session > default
        user_id: getUserId(session?.user_id),
        channel: getChannel(session?.channel),
        // ==================== userId 统一整改结束 ====================
        stream: true,
        mode: getPlanModeForRequest(planModeEnabled),
        goal_mode_enabled: goalModeEnabled,
        ...biz_params,
        context_references:
          userText.startsWith("/") &&
          pendingContextReferencesRef.current.length === 0
            ? []
            : pendingContextReferencesRef.current,
        scenario_preset_id: pendingScenarioPresetIdRef.current || undefined,
        file_url_network: resolveCurrentFileUrlNetwork(),
        selected_expert_id: selectedExpertId || undefined,
      };
      pendingContextReferencesRef.current = [];

      const backendChatId = resolveRequestChatId(
        {
          session_id,
          logical_session_id: resolvedLogicalSessionId,
          chat_id,
        },
        requestBody.session_id,
      );
      let routedAsSteering = false;
      if (backendChatId && userText) {
        try {
          const activeGoal = await chatApi.getRecentGoal(backendChatId);
          if (
            activeGoal &&
            shouldRouteGoalRequestAsSteering({
              goalState: activeGoal.state,
              hasExplicitGoalId: Object.prototype.hasOwnProperty.call(
                biz_params ?? {},
                "goal_id",
              ),
            })
          ) {
            await chatApi.enqueueGoalSteering(
              activeGoal.goal_id,
              backendChatId,
              userText,
            );
            routedAsSteering = true;
          }
        } catch (error) {
          console.warn("Unable to route input to active Goal:", error);
        }
      }
      if (backendChatId) {
        if (userText) {
          sessionApi.setLastUserMessage(backendChatId, userText);
        }
      }
      if (routedAsSteering) {
        return new Response(
          `data: ${JSON.stringify({
            object: "response",
            status: "completed",
            output: [],
            id: `goal-steering-${Date.now()}`,
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }

      const timeoutSignal = createTimedAbortSignal(data.signal);
      // The expert is a one-turn selection. Clear it as soon as the request
      // has been submitted so aborts/network failures cannot leave stale UI
      // state for the next turn.
      setSelectedExpertId(null);
      setSubAgentMonitorResetKey((value) => value + 1);
      try {
        const response = await fetch(getApiUrl("/console/chat"), {
          method: "POST",
          headers,
          body: JSON.stringify(requestBody),
          signal: timeoutSignal.signal,
        });

        if (shouldClearPendingScenarioPreset(response.status)) {
          pendingScenarioPresetIdRef.current = null;
        }

        return response;
      } finally {
        timeoutSignal.cleanup();
      }
    },
    [
      loadActiveModelData,
      planModeEnabled,
      goalModeEnabled,
      resolveLogicalRequestSessionId,
      resolveRequestChatId,
      selectedAgent,
      selectedExpertId,
    ],
  );

  const handleFileUpload = useCallback(
    async (options: {
      file: File;
      onSuccess: (body: { url?: string; thumbUrl?: string }) => void;
      onError?: (e: Error) => void;
      onProgress?: (e: { percent?: number }) => void;
    }) => {
      await uploadChatAttachment({
        ...options,
        message,
        t,
        multimodalCaps,
        maxUploadMb: CHAT_ATTACHMENT_MAX_MB,
        uploadFile: chatApi.uploadFile,
        filePreviewUrl: chatApi.filePreviewUrl,
      });
    },
    [message, multimodalCaps, t],
  );

  // ==================== Drag & drop file upload (Kun He) ====================
  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.types.includes("Files")) {
      dragCounterRef.current += 1;
      if (dragCounterRef.current === 1) {
        setIsDragging(true);
      }
    }
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current -= 1;
    if (dragCounterRef.current === 0) {
      setIsDragging(false);
    }
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current = 0;
    setIsDragging(false);

    const files = Array.from(e.dataTransfer.files);
    for (const file of files) {
      document.dispatchEvent(
        new CustomEvent("pasteFile", {
          detail: { file },
        }),
      );
    }
  }, []);

  const handleDragOverlayClose = useCallback(() => {
    dragCounterRef.current = 0;
    setIsDragging(false);
  }, []);
  // ==================== Drag & drop end ====================

  const feedbackRenderContextValue = useMemo<ChatFeedbackRenderContextValue>(
    () => ({
      feedbackChatId,
      feedbackLookup,
      feedbackLookupPending,
      feedbackSessionId,
      feedbackTask,
      onFeedbackSaved: handleFeedbackSaved,
    }),
    [
      feedbackChatId,
      feedbackLookup,
      feedbackLookupPending,
      feedbackSessionId,
      feedbackTask,
      handleFeedbackSaved,
    ],
  );
  const planReviewRenderContextValue =
    useMemo<ChatPlanReviewRenderContextValue>(
      () => ({
        onContinueModifying: handleContinueModifyingPlan,
        onPlanModeDecision: handlePlanModeDecision,
        onConfirmGoalProposal: async (proposal) => {
          if (!feedbackChatId) {
            throw new Error("当前会话尚未创建，无法确认 Goal");
          }
          return chatApi.createGoal(feedbackChatId, {
            objective: proposal.objective,
            completion_criteria: proposal.completion_criteria,
            constraints: proposal.constraints,
            autonomy_boundary: proposal.autonomy_boundary,
          });
        },
      }),
      [feedbackChatId, handleContinueModifyingPlan, handlePlanModeDecision],
    );
  const handleGoalResume = useCallback((goalId: string) => {
    emit({
      type: "handleSubmit",
      data: {
        query: "继续已恢复的 Goal",
        fileList: [],
        biz_params: { mode: "normal", goal_id: goalId },
      },
    });
  }, []);
  const htmlPreviewTrackingContextValue = useMemo(
    () => ({
      cronTaskId: feedbackTask?.cronTaskId || null,
      cronTaskName: feedbackTask?.cronTaskName || null,
      disableEventRecording: skipPreviewTracking,
    }),
    [feedbackTask, skipPreviewTracking],
  );

  const options = useMemo(() => {
    const i18nConfig = getDefaultConfig(
      t,
    ) as unknown as Partial<IAgentScopeRuntimeWebUIOptions>;
    const commandSuggestions: CommandSuggestion[] = [
      {
        command: "/clear",
        value: "clear",
        description: t("chat.commands.clear.description"),
      },
      {
        command: "/compact",
        value: "compact",
        description: t("chat.commands.compact.description"),
      },
      {
        command: "/approve",
        value: "approve",
        description: t("chat.commands.approve.description"),
      },
      {
        command: "/deny",
        value: "deny",
        description: t("chat.commands.deny.description"),
      },
      {
        command: "/plan",
        value: "plan",
        description: t("chat.commands.plan.description", "进入计划模式"),
      },
    ];

    const senderConfig = i18nConfig.sender as
      | IAgentScopeRuntimeWebUISenderOptions
      | undefined;
    const contextUsageIndicator = <ContextUsageIndicator {...contextUsage} />;
    const senderPrefixNodes = Children.toArray([
      activePlanModeControl,
      activeGoalModeControl,
      activeExpertControl,
      senderConfig?.prefix,
    ]).filter(Boolean);

    const { beforeSubmit: handleSkillMentionsBeforeSubmit, skillMentions } =
      createWelcomeSkillMentions({
        contextReferences,
        contextReferencesError,
        contextReferencesLoading,
        isComposingRef,
        loadContextReferences,
        pendingContextReferencesRef,
        selectedContextReferences,
        setSelectedContextReferences,
      });

    const handleBeforeSubmit: NonNullable<
      IAgentScopeRuntimeWebUISenderOptions["beforeSubmit"]
    > = async (data) => {
      if (isComposingRef.current) return false;
      const skillPrepared = await handleSkillMentionsBeforeSubmit(data);
      if (skillPrepared === false) return false;
      const prepared = await preparePlanModeSubmit(skillPrepared, {
        planModeEnabled,
        persistPlanMode,
        setPlanModeEnabled: setPlanModeEnabledForActiveScope,
      });
      if (isPlanModeSubmitCancelled(prepared)) {
        return prepared;
      }
      const hasExplicitPlanInteractionResponse = Boolean(
        prepared.biz_params &&
          Object.prototype.hasOwnProperty.call(
            prepared.biz_params,
            "plan_interaction_response",
          ),
      );
      if (hasExplicitPlanInteractionResponse) {
        setPendingPlanRevision(null);
        return prepared;
      }
      if (!pendingPlanRevision) {
        return prepared;
      }

      const feedback = prepared.query.trim();
      if (!feedback) {
        return false;
      }

      setPendingPlanRevision(null);
      return {
        ...prepared,
        biz_params: {
          ...(prepared.biz_params || {}),
          mode: "plan",
          plan_interaction_response: {
            card_type: "plan_review",
            plan_id: pendingPlanRevision.planId,
            decision: "revise",
            feedback,
          },
        },
      };
    };

    const planModeQuickMenuItems = [
      <ComposerQuickMenuSubmenu
        key="mode"
        icon={<ControlOutlined />}
        label={t("chat.quickMenu.mode", "模式")}
        disabled={composerDisabled}
      >
        <PlanModeMenuItem
          key="plan-mode"
          ariaLabel={t("chat.planMode.label", "计划模式")}
          enabled={planModeEnabled}
          disabled={composerDisabled}
          label={t("chat.planMode.shortLabel", "计划")}
          showIcon={false}
          tooltip={t("chat.planMode.tooltip", "计划模式使用只读工具先产出计划")}
          onChange={(enabled) => {
            if (enabled) setGoalModeEnabled(false);
            void persistPlanMode(enabled);
          }}
        />
        <ComposerQuickMenuItem
          key="goal-mode"
          interactive
          label="目标"
          extra={
            <Switch
              size="small"
              checked={goalModeEnabled}
              disabled={composerDisabled}
              aria-label="目标模式"
              onChange={(enabled) => {
                setGoalModeEnabled(enabled);
                if (enabled) {
                  setSelectedExpertId(null);
                  if (planModeEnabled) void persistPlanMode(false);
                }
              }}
            />
          }
        />
      </ComposerQuickMenuSubmenu>,
      ...(expertsLoading || experts.length > 0
        ? [
            <ComposerQuickMenuSubmenu
              key="expert"
              icon={<TeamOutlined />}
              label="专家"
              disabled={composerDisabled || goalModeEnabled || expertsLoading}
              panelWidth="min(240px, calc(100vw - 32px))"
            >
              <ExpertSelector
                experts={experts}
                loading={expertsLoading}
                planModeEnabled={planModeEnabled}
                goalModeEnabled={goalModeEnabled}
                selectedExpertId={selectedExpertId}
                onChange={setSelectedExpertId}
                onDisablePlanMode={() => {
                  void persistPlanMode(false);
                }}
                disabled={composerDisabled}
                inline
              />
            </ComposerQuickMenuSubmenu>,
          ]
        : []),
    ];

    return {
      ...i18nConfig,
      theme: {
        ...defaultConfig.theme,
        darkMode: isDark,
        leftHeader: {
          ...defaultConfig.theme.leftHeader,
        },
        rightHeader: (
          <>
            <ChatSessionInitializer />
            <RuntimeLoadingBridge bridgeRef={runtimeLoadingBridgeRef} />
            <ChatHeaderTitle />
            <span style={{ flex: 1 }} />
            {!isContentOnly && <FileManager />}
            {!isContentOnly && <ChatActionGroup chatId={chatId} />}
            {!isContentOnly && <ModelSelector />}
          </>
        ),
      },
      welcome: {
        ...i18nConfig.welcome,
        nick: brandTheme.brandName,
        // ==================== 品牌主题 (Kun He) ====================
        // 使用动态品牌 avatar
        avatar: brandTheme.avatar
          ? `${import.meta.env.BASE_URL}${brandTheme.avatar.replace(/^\//, "")}`
          : undefined,
        // ==================== 品牌主题结束 ====================
        // ==================== 首页改版 (Kun He) ====================
        // 使用自定义欢迎页渲染，替代默认 WelcomePrompts
        render: ({ greeting, onSubmit }) => (
          <WelcomeCenterLayout
            greeting={
              typeof greeting === "string" ? greeting : "你好，有什么可以帮您？"
            }
            placeholder={t("chat.inputPlaceholder")}
            beforeSubmit={handleBeforeSubmit}
            quickMenuItems={planModeQuickMenuItems}
            prefixItems={
              <>
                {activePlanModeControl}
                {activeGoalModeControl}
                {contextUsageIndicator}
              </>
            }
            onSubmit={(data) => onSubmit(data)}
            onScenarioPresetSubmit={(scenarioPresetId) => {
              pendingScenarioPresetIdRef.current = scenarioPresetId;
            }}
            skillMentions={skillMentions}
          />
        ),
        // ==================== 首页改版结束 ====================
      },
      sender: {
        ...senderConfig,
        beforeSubmit: handleBeforeSubmit,
        beforeUI: (
          <>
            {taskProgressEnabled ? (
              <TaskProgressFloatingCard progress={taskProgress} />
            ) : null}
          </>
        ),
        renderComposer: (defaultComposer) => (
          <ActivePlanInteractionComposer
            defaultComposer={cloneElement<IChatInputProps>(defaultComposer, {
              actions: (defaultActions) => (
                <div className={styles.composerActions}>
                  {contextUsageIndicator}
                  {defaultActions}
                </div>
              ),
            })}
            onContinueModifying={handleContinueModifyingPlan}
            onPlanModeDecision={handlePlanModeDecision}
          />
        ),
        quickMenuItems: planModeQuickMenuItems,
        prefix:
          senderPrefixNodes.length > 0 ? <>{senderPrefixNodes}</> : undefined,
        allowSpeech: true,
        attachments: {
          accept: CHAT_ATTACHMENT_ACCEPT_HINT,
          customRequest: handleFileUpload,
        },
        placeholder: t("chat.inputPlaceholder"),
        suggestions: commandSuggestions.map((item) => ({
          label: renderSuggestionLabel(item.command, item.description),
          value: item.value,
        })),
        skillMentions,
      },
      session: {
        multiple: true,
        hideBuiltInSessionList: true,
        api: sessionApi,
      },
      cards: chatCardRenderers,
      api: {
        ...defaultConfig.api,
        fetch: customFetch,
        replaceMediaURL: (url: string) => {
          return toDisplayUrl(url);
        },
        cancel(data: {
          session_id: string;
          logical_session_id?: string;
          chat_id?: string | null;
          msgid?: string | null;
        }) {
          const logicalSessionId = resolveLogicalRequestSessionId(data);
          const chatId = resolveRequestChatId(data, logicalSessionId);
          if (chatId) {
            return chatApi
              .stopChat(chatId, data.msgid, logicalSessionId)
              .catch((err) => {
                console.error("Failed to stop chat:", err);
              });
          }
          return Promise.resolve();
        },
        async reconnect(data: {
          session_id: string;
          signal?: AbortSignal;
          logical_session_id?: string;
          chat_id?: string | null;
        }) {
          const headers: Record<string, string> = {
            "Content-Type": "application/json",
            ...buildAuthHeaders(),
          };
          const logicalSessionId = resolveLogicalRequestSessionId(data);
          const reconnectSessionId = resolveRequestChatId(
            data,
            logicalSessionId,
          );

          const timeoutSignal = createTimedAbortSignal(data.signal);
          try {
            return await fetch(getApiUrl("/console/chat"), {
              method: "POST",
              headers,
              body: JSON.stringify({
                reconnect: true,
                reconnect_mode: "current",
                session_id: reconnectSessionId,
                chat_id: data.chat_id || undefined,
                // ==================== userId 统一整改 (Kun He) ====================
                // 使用 getUserId()/getChannel() 获取
                user_id: getUserId(),
                channel: getChannel(),
                // ==================== userId 统一整改结束 ====================
                mode: getPlanModeForRequest(planModeEnabled),
              }),
              signal: timeoutSignal.signal,
            });
          } finally {
            timeoutSignal.cleanup();
          }
        },
      },
      // ==================== 自定义工具渲染器 ====================
      customToolRenderConfig: {
        copy_file_to_static: CopyFileToStatic,
      },
      // ==================== 自定义工具渲染器结束 ====================
      actions: {
        list: [
          {
            icon: (
              <span title={t("common.copy")}>
                <SparkCopyLine />
              </span>
            ),
            onClick: ({ data }: { data: CopyableResponse }) => {
              void copyResponse(data);
            },
          },
        ],
        replace: true,
      },
    } as unknown as IAgentScopeRuntimeWebUIOptions;
  }, [
    activeGoalModeControl,
    activeExpertControl,
    activePlanModeControl,
    brandTheme.avatar,
    brandTheme.brandName,
    customFetch,
    copyResponse,
    chatId,
    activeSessionId,
    feedbackChatId,
    handleFileUpload,
    handleContinueModifyingPlan,
    handlePlanModeDecision,
    isComposingRef,
    isContentOnly,
    isDark,
    experts,
    expertsLoading,
    multimodalCaps,
    composerDisabled,
    composerLoading,
    contextUsageChatId,
    contextUsage,
    pendingPlanRevision,
    persistPlanMode,
    planModeEnabled,
    resolveLogicalRequestSessionId,
    resolveRequestChatId,
    setPlanModeEnabledForActiveScope,
    selectedExpertId,
    selectedContextReferences,
    contextReferences,
    contextReferencesError,
    contextReferencesLoading,
    loadContextReferences,
    taskProgress,
    taskProgressEnabled,
    subAgentMonitorResetKey,
    t,
  ]);

  // ==================== 首页改版 (Kun He) ====================
  // 新建聊天：通过 chatRef 调用后端 createSession API
  const handleCreateSessionFromSidebar = useCallback(async () => {
    const newId = await chatRef.current?.createSession?.();
    if (newId) {
      navigate(`/chat/${newId}`, { replace: true });
    } else {
      navigate("/chat", { replace: true });
    }
  }, [navigate]);
  // ==================== 首页改版结束 ====================

  // 定义 cards 配置（与 AgentScopeRuntimeWebUI 内部一致）
  const cards = useMemo(() => {
    return {
      AgentScopeRuntimeRequestCard,
      AgentScopeRuntimeResponseCard,
      ...options.cards,
    };
  }, [options.cards]);

  return (
    <ChatShareSelectionProvider>
      <AgentScopeRuntimeWebUIComposedProvider options={options} cards={cards}>
        <ChatFeedbackRenderProvider value={feedbackRenderContextValue}>
          <HtmlPreviewTrackingProvider value={htmlPreviewTrackingContextValue}>
            <AutoPreviewHtmlProvider
              triggerKey={autoPreviewTriggerKey}
              onConsumed={() => setAutoPreviewTriggerKey(0)}
            >
              <div
                data-chat-shell
                style={{
                  height: "100%",
                  width: "100%",
                  display: "flex",
                  flexDirection: "row",
                }}
              >
                {/* ==================== 首页改版 (Kun He) ==================== */}
                {/* 聊天专用侧栏：支持折叠为64px工具条 */}
                {!isContentOnly && (
                  <ChatSidebar
                    tasks={tasks}
                    selectedTaskId={currentTask?.id}
                    onCreateSession={handleCreateSessionFromSidebar}
                    onTaskClick={handleTaskOpen}
                    onTaskPause={handleTaskPause}
                    onTaskRun={handleTaskRun}
                    onTaskResume={handleTaskResume}
                    onTaskDelete={handleTaskDelete}
                    onTaskEdit={handleTaskEdit}
                  />
                )}
                {/* ==================== 首页改版结束 ==================== */}
                <div
                  className={styles.chatMessagesArea}
                  data-chat-messages-area
                  style={{ flex: 1, minWidth: 0, position: "relative" }}
                  onDragEnter={isContentOnly ? undefined : handleDragEnter}
                  onDragLeave={isContentOnly ? undefined : handleDragLeave}
                  onDragOver={isContentOnly ? undefined : handleDragOver}
                  onDrop={isContentOnly ? undefined : handleDrop}
                >
                  <ChatContentOnlyProvider enabled={isContentOnly}>
                    <ChatPlanReviewRenderProvider
                      value={planReviewRenderContextValue}
                    >
                      <GlobalVoiceRecorder enabled={voiceRecorderEnabled}>
                        <WPlusSopActiveBar
                          chatId={feedbackChatId || chatId}
                          logicalSessionId={feedbackSessionId || undefined}
                          onLocksChatInputChange={setWPlusSopLocksChatInput}
                        />
                        <div
                          className={
                            wPlusSopLocksChatInput
                              ? styles.chatDisabledOverlay
                              : undefined
                          }
                          style={{ height: "100%", width: "100%" }}
                        >
                          <FilePreviewPresentationProvider
                            value={
                              feedbackTask?.cronTaskId ? "modal" : "workspace"
                            }
                          >
                            <AgentScopeRuntimeWebUILayout ref={chatRef} />
                          </FilePreviewPresentationProvider>
                        </div>
                      </GlobalVoiceRecorder>
                    </ChatPlanReviewRenderProvider>
                  </ChatContentOnlyProvider>
                  <SubAgentRunMonitor
                    chatId={feedbackChatId}
                    resetKey={subAgentMonitorResetKey}
                  />
                  <GoalMonitor
                    chatId={feedbackChatId}
                    onResume={handleGoalResume}
                  />
                  {!isContentOnly && (
                    <DragUploadOverlay
                      visible={isDragging}
                      onClose={handleDragOverlayClose}
                    />
                  )}
                  <ConversationQuickNav />
                </div>
              </div>
            </AutoPreviewHtmlProvider>
          </HtmlPreviewTrackingProvider>
        </ChatFeedbackRenderProvider>

        <Modal
          open={Boolean(editingTask)}
          title="编辑任务"
          width="min(760px, calc(100vw - 32px))"
          className={styles.taskEditModal}
          centered
          destroyOnClose
          maskClosable={!taskEditSaving}
          keyboard={!taskEditSaving}
          onCancel={handleTaskEditClose}
          footer={
            <div className={styles.taskEditModalFooter}>
              <Button onClick={handleTaskEditClose} disabled={taskEditSaving}>
                取消
              </Button>
              <Button
                type="primary"
                loading={taskEditSaving}
                onClick={() => taskEditForm.submit()}
              >
                保存
              </Button>
            </div>
          }
        >
          <Form
            form={taskEditForm}
            layout="vertical"
            onFinish={() =>
              handleTaskEditSubmit(
                taskEditForm.getFieldsValue(true) as CronTaskEditFormValues,
              )
            }
            initialValues={DEFAULT_FORM_VALUES}
            className={styles.taskEditForm}
          >
            <ChatTaskEditFormBody />
          </Form>
        </Modal>

        <Modal
          open={showModelPrompt}
          closable={false}
          footer={null}
          width={480}
          styles={{
            content: isDark
              ? {
                  background: "#1f1f1f",
                  boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
                }
              : undefined,
          }}
        >
          <Result
            icon={<ExclamationCircleOutlined style={{ color: "#faad14" }} />}
            title={
              <span
                style={{ color: isDark ? "rgba(255,255,255,0.88)" : undefined }}
              >
                {t("modelConfig.promptTitle")}
              </span>
            }
            subTitle={
              <span
                style={{ color: isDark ? "rgba(255,255,255,0.55)" : undefined }}
              >
                {t("modelConfig.promptMessage")}
              </span>
            }
            extra={[
              <Button key="skip" onClick={() => setShowModelPrompt(false)}>
                {t("modelConfig.skipButton")}
              </Button>,
              <Button
                key="configure"
                type="primary"
                icon={<SettingOutlined />}
                onClick={() => {
                  setShowModelPrompt(false);
                  navigate("/models");
                }}
              >
                {t("modelConfig.configureButton")}
              </Button>,
            ]}
          />
        </Modal>
      </AgentScopeRuntimeWebUIComposedProvider>
    </ChatShareSelectionProvider>
  );
}
