import { useState, useCallback, useRef, useMemo, useEffect } from "react";
import { flushSync } from "react-dom";
import { useNavigate, useLocation } from "react-router-dom";
import { Image, Modal, Tooltip } from "antd";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useContextSelector } from "use-context-selector";
import useChatAnywhereEventEmitter from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Context/useChatAnywhereEventEmitter";
import Style from "./style";
import ChatTaskList from "../ChatTaskList";
import type { CronJobSpecOutput } from "@/api/types";
import { DESIGN_TOKENS } from "@/config/designTokens";
import CollapsedToolbar, { type PanelType } from "./CollapsedToolbar";
import ExpandablePanel from "./ExpandablePanel";
import type { HistorySession } from "./historySessions";
import { isHistorySessionActive } from "./historySessions";
import sendIcon from "../../../../assets/icons/new_chat.svg";
import operateIcon from "../../../../assets/icons/operate.svg";
import shGuideImage from "@/assets/others/sh_note.png";
import {
  ChatAnywhereSessionsContext,
  type IAgentScopeRuntimeWebUISession,
} from "@/components/agentscope-chat";
import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";
import sessionApi from "../../sessionApi";
import { getSessionAgentId } from "../../sessionApi/sessionAgent";
import { resolveRequestedSessionId } from "../../sessionApi/resolvedSessionMapping";
import { HistorySessionRow } from "./HistorySessionRow";
import { HistorySkeleton } from "./HistorySkeleton";
import { HistoryInfiniteScrollTrigger } from "./HistoryInfiniteScrollTrigger";
import { mergeConcurrentSessions } from "./mergeConcurrentSessions";

const OPERATION_GUIDE_BBK = "121";
const OPERATION_GUIDE_SOURCE = "RMASSIST";
// 临时占位链接，待操作指南正式地址确认后替换。
const OPERATION_GUIDE_URL = "https://example.com";
const SPECIAL_SOURCE = new Set(["ruice", "pfmind"]);

/** Extended session type with additional backend fields */
interface ExtendedHistorySession extends HistorySession {
  channel?: string;
  realId?: string;
}

type SessionIdentity = Partial<IAgentScopeRuntimeWebUISession> & {
  realId?: string;
};

function HistoryIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <circle
        cx="8"
        cy="8"
        r="6"
        stroke={DESIGN_TOKENS.colorTextPrimary}
        strokeWidth="1.5"
      />
      <path
        d="M8 4.5V8L10.5 9.5"
        stroke={DESIGN_TOKENS.colorTextPrimary}
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

function ToggleIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg
      width="10"
      height="6"
      viewBox="0 0 10 6"
      fill="none"
      className={`chat-sidebar-history-toggle${
        collapsed ? " chat-sidebar-history-toggle--collapsed" : ""
      }`}
    >
      <path
        d="M1 1L5 5L9 1"
        stroke={DESIGN_TOKENS.colorTextMuted}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export interface ChatSidebarProps {
  tasks: CronJobSpecOutput[];
  selectedTaskId?: string;
  onCreateSession?: () => void;
  onTaskClick?: (task: CronJobSpecOutput) => void;
  onTaskPause?: (task: CronJobSpecOutput) => void;
  onTaskRun?: (task: CronJobSpecOutput) => void;
  onTaskResume?: (task: CronJobSpecOutput) => void;
  onTaskDelete?: (task: CronJobSpecOutput) => void;
  onTaskEdit?: (task: CronJobSpecOutput) => void;
}

export default function ChatSidebar(props: ChatSidebarProps) {
  const {
    tasks,
    selectedTaskId,
    onCreateSession,
    onTaskClick,
    onTaskPause,
    onTaskRun,
    onTaskResume,
    onTaskDelete,
    onTaskEdit,
  } = props;
  const navigate = useNavigate();
  const location = useLocation();
  const [historyCollapsed, setHistoryCollapsed] = useState(false);
  const [isLoadingMoreHistory, setIsLoadingMoreHistory] = useState(false);
  const [loadMoreHistoryFailed, setLoadMoreHistoryFailed] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [activePanel, setActivePanel] = useState<PanelType>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const historyScrollContainerRef = useRef<HTMLDivElement>(null);
  // Guide image preview state
  const [guidePreviewVisible, setGuidePreviewVisible] = useState(false);
  // const { sessions: sharedSessions, setSessionLoading, setSessions } = useChatAnywhereSessionsState();
  const sharedSessions = useContextSelector(
    ChatAnywhereSessionsContext,
    (value) => value.sessions,
  );
  const setSessionLoading = useContextSelector(
    ChatAnywhereSessionsContext,
    (value) => value.setSessionLoading,
  );
  const setSessions = useContextSelector(
    ChatAnywhereSessionsContext,
    (value) => value.setSessions,
  );
  const getSessions = useContextSelector(
    ChatAnywhereSessionsContext,
    (value) => value.getSessions,
  );
  const isSessionsListLoading = useContextSelector(
    ChatAnywhereSessionsContext,
    (value) => value.isSessionsListLoading,
  );
  const { selectedAgent, setSelectedAgent } = useAgentStore();
  const bbk = useIframeStore((state) => state.bbk);
  const source = useIframeStore((state) => state.source);
  const isOriginY = useIframeStore((state) => state.isOriginY);
  const isOperationGuideContext =
    bbk === OPERATION_GUIDE_BBK && source === OPERATION_GUIDE_SOURCE;
  const isHeaderHidden = SPECIAL_SOURCE.has(source ?? "");

  const currentChatId = location.pathname.match(/^\/chat\/(.+)$/)?.[1] || null;
  const currentChatIdRef = useRef<string | null>(currentChatId);
  currentChatIdRef.current = currentChatId;

  // 刷新共享 sessions 状态
  const refreshSessions = useCallback(
    async (excludedSessions: SessionIdentity[] = []) => {
      try {
        const sessionList = await sessionApi.getSessionList();
        setSessions(
          mergeConcurrentSessions(
            sessionList,
            getSessions(),
            false,
            excludedSessions,
          ),
        );
        setLoadMoreHistoryFailed(false);
      } catch {
        // ignore
      }
    },
    [getSessions, setSessions],
  );

  const loadMoreSessions = useCallback(async () => {
    if (isLoadingMoreHistory || !sessionApi.hasMoreSessions()) return;
    setIsLoadingMoreHistory(true);
    setLoadMoreHistoryFailed(false);
    try {
      const sessionList = await sessionApi.loadMoreSessions();
      setSessions(mergeConcurrentSessions(sessionList, getSessions(), true));
    } catch {
      setLoadMoreHistoryFailed(true);
    } finally {
      setIsLoadingMoreHistory(false);
    }
  }, [getSessions, isLoadingMoreHistory, setSessions]);

  // 监听 visibilitychange 刷新 sessions
  useEffect(() => {
    const handleVisibilityRefresh = () => {
      if (document.visibilityState === "visible") {
        void refreshSessions();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityRefresh);

    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityRefresh);
    };
  }, [refreshSessions]);

  // 异步标题生成完成后刷新侧边栏
  useChatAnywhereEventEmitter(
    {
      type: "refreshSessionList",
      callback: () => {
        void refreshSessions();
      },
    },
    [refreshSessions],
  );

  // 使用共享 sessions 状态，过滤并转换
  const sessions = useMemo(() => {
    return sharedSessions
      .filter((s) => (s as HistorySession).meta?.session_kind !== "task")
      .map((s) => {
        const hs = s as HistorySession;
        return {
          ...hs,
          createdAt: hs.createdAt || new Date(parseInt(s.id)).toISOString(),
        };
      });
  }, [sharedSessions]);
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;
  const historyTotal = Math.max(
    sessionApi.getSessionTotal() - tasks.length,
    sessions.length,
  );

  const handleToggleHistory = useCallback(() => {
    setHistoryCollapsed((prev) => !prev);
  }, []);

  const handleSessionClick = useCallback(
    (sessionId: string) => {
      // 使用 resolveRequestedSessionId 解析 sessionId，处理临时时间戳ID到真实UUID的映射
      const resolvedId = resolveRequestedSessionId({
        requestedSessionId: sessionId,
        sessionList: sessionsRef.current,
      });
      const effectiveSessionId = resolvedId || sessionId;

      const targetSession = sessionsRef.current.find(
        (session) =>
          session.id === effectiveSessionId ||
          (session as HistorySession).realId === effectiveSessionId,
      );
      if (isHistorySessionActive(targetSession, currentChatIdRef.current))
        return;

      const sessionAgentId = getSessionAgentId(targetSession?.meta);
      if (sessionAgentId && sessionAgentId !== selectedAgent) {
        setSelectedAgent(sessionAgentId);
      }

      // Force loading to render immediately before navigate triggers re-render
      flushSync(() => {
        setSessionLoading(true);
      });

      navigate(`/chat/${sessionId}`, { replace: true });
    },
    [navigate, selectedAgent, setSelectedAgent, setSessionLoading],
  );

  const handleNewTopic = useCallback(() => {
    onCreateSession?.();
  }, [onCreateSession]);

  const handleToggleCollapse = useCallback(() => {
    setCollapsed((prev) => !prev);
    setActivePanel(null);
  }, []);

  const handleNewChat = useCallback(() => {
    setActivePanel(null);
    onCreateSession?.();
  }, [onCreateSession]);

  const handleIconClick = useCallback((panel: PanelType) => {
    setActivePanel(panel);
  }, []);

  const handleClosePanel = useCallback(() => {
    setActivePanel(null);
  }, []);

  const handleTaskOpen = useCallback(
    (task: CronJobSpecOutput) => {
      setActivePanel(null);
      onTaskClick?.(task);
    },
    [onTaskClick],
  );

  const handleOpenGuide = useCallback(() => {
    if (isOperationGuideContext) {
      setGuidePreviewVisible(true);
      return;
    }

    window.open(OPERATION_GUIDE_URL, "_blank", "noopener,noreferrer");
  }, [isOperationGuideContext]);

  const handleDeleteSession = useCallback(
    (
      sessionId: string,
      backendId: string | null,
      sessionName: string = "新会话",
    ) => {
      Modal.confirm({
        title: "删除历史记录",
        content: `确认删除会话“${sessionName}”？删除后无法恢复。`,
        centered: true,
        okText: "删除",
        okType: "danger",
        cancelText: "取消",
        cancelButtonProps: { type: "text" },
        onOk: async () => {
          const removedSession = backendId
            ? { id: sessionId, realId: backendId }
            : { id: sessionId };
          await sessionApi.removeSession({ id: sessionId });

          if (currentChatIdRef.current === sessionId) {
            const next = sessionsRef.current.filter((s) => s.id !== sessionId);
            if (next[0]?.id) {
              navigate(`/chat/${next[0].id}`, { replace: true });
            } else {
              navigate("/chat", { replace: true });
            }
          }

          await refreshSessions([removedSession]);
        },
      });
    },
    [refreshSessions, navigate],
    // [sessions, currentChatId, refreshSessions, navigate],
  );

  if (collapsed) {
    // Calculate total unread execution count for badge
    const unreadCount = tasks.reduce(
      (sum, task) => sum + (task.task?.unread_execution_count || 0),
      0,
    );

    return (
      <>
        <Style />
        <div className="chat-sidebar-wrapper chat-sidebar-wrapper--collapsed">
          <div ref={toolbarRef}>
            <CollapsedToolbar
              activePanel={activePanel}
              onIconClick={handleIconClick}
              onNewChat={handleNewChat}
              taskBadgeCount={unreadCount}
            />
          </div>
          <ExpandablePanel
            visible={activePanel === "tasks"}
            type="tasks"
            onClose={handleClosePanel}
            tasks={tasks}
            selectedTaskId={selectedTaskId}
            sessions={sessions}
            onTaskClick={handleTaskOpen}
            onTaskPause={onTaskPause}
            onTaskRun={onTaskRun}
            onTaskResume={onTaskResume}
            onTaskDelete={onTaskDelete}
            onTaskEdit={onTaskEdit}
            toolbarRef={toolbarRef}
          />
          <ExpandablePanel
            visible={activePanel === "history"}
            type="history"
            onClose={handleClosePanel}
            tasks={tasks}
            sessions={sessions}
            onTaskClick={handleTaskOpen}
            toolbarRef={toolbarRef}
            hasMoreSessions={sessionApi.hasMoreSessions()}
            sessionTotal={historyTotal}
            isLoadingMoreSessions={isLoadingMoreHistory}
            loadMoreSessionsFailed={loadMoreHistoryFailed}
            onLoadMoreSessions={() => void loadMoreSessions()}
          />
          <Tooltip title="展开导航" placement="right">
            <button
              className="chat-sidebar-collapse-toggle"
              onClick={handleToggleCollapse}
              type="button"
              aria-label="展开导航"
            >
              <PanelLeftOpen aria-hidden="true" size={18} strokeWidth={1.8} />
            </button>
          </Tooltip>
        </div>
      </>
    );
  }

  return (
    <>
      <Style />
      <div className="chat-sidebar-wrapper">
        <div className="chat-sidebar">
          <div className="chat-sidebar-content">
            <div className="chat-sidebar-new-topic">
              <button
                className="chat-sidebar-new-topic-btn"
                onClick={handleNewTopic}
                type="button"
              >
                <img src={sendIcon} alt="+" width="16" height="16" />
                新建会话
              </button>
              <Tooltip title="收起导航" placement="right">
                <button
                  className="chat-sidebar-collapse-toggle chat-sidebar-collapse-toggle--expanded"
                  onClick={handleToggleCollapse}
                  type="button"
                  aria-label="收起导航"
                >
                  <PanelLeftClose
                    aria-hidden="true"
                    size={18}
                    strokeWidth={1.8}
                  />
                </button>
              </Tooltip>
            </div>
            <div
              className={`chat-sidebar-content-record-list${
                isHeaderHidden
                  ? " chat-sidebar-content-record-list--without-header"
                  : ""
              }`}
              ref={historyScrollContainerRef}
            >
              <ChatTaskList
                tasks={tasks}
                selectedTaskId={selectedTaskId}
                onTaskClick={handleTaskOpen}
                onTaskPause={onTaskPause}
                onTaskRun={onTaskRun}
                onTaskResume={onTaskResume}
                onTaskDelete={onTaskDelete}
                onTaskEdit={onTaskEdit}
              />
              <div className="chat-sidebar-history">
                <div
                  className="chat-sidebar-history-header"
                  onClick={handleToggleHistory}
                  role="button"
                  tabIndex={0}
                >
                  <div className="chat-sidebar-history-title">
                    <HistoryIcon />
                    历史记录({historyTotal})
                  </div>
                  <ToggleIcon collapsed={historyCollapsed} />
                </div>
                {!historyCollapsed &&
                  (isSessionsListLoading ? (
                    <HistorySkeleton count={8} />
                  ) : (
                    <div className="chat-sidebar-history-list">
                      {sessions.length === 0 ? (
                        <div className="chat-sidebar-history-empty">
                          暂无历史记录
                        </div>
                      ) : (
                        sessions.map((session) => {
                          const ext = session as ExtendedHistorySession;
                          return (
                            <HistorySessionRow
                              key={session.id}
                              name={session.name || "新会话"}
                              session={ext}
                              active={isHistorySessionActive(
                                ext,
                                currentChatId,
                              )}
                              onSessionClick={handleSessionClick}
                              onSessionDelete={handleDeleteSession}
                            />
                          );
                        })
                      )}
                      <HistoryInfiniteScrollTrigger
                        scrollContainerRef={historyScrollContainerRef}
                        hasMore={sessionApi.hasMoreSessions()}
                        loading={isLoadingMoreHistory}
                        failed={loadMoreHistoryFailed}
                        onLoadMore={() => void loadMoreSessions()}
                      />
                    </div>
                  ))}
              </div>
            </div>
          </div>

          {isOriginY && (
            <div className="chat-sidebar-footer">
              {/* 暂时隐藏，后续需要时再开放
              <div className="chat-sidebar-footer-item">
                <img src={skillMarketIcon} alt="发送" width="24" height="24" />
                skill市场
              </div>
              <div className="chat-sidebar-footer-divider" /> */}
              <div
                className="chat-sidebar-footer-item"
                onClick={handleOpenGuide}
                role="button"
                tabIndex={0}
              >
                <img src={operateIcon} alt="note" width="20" height="20" />
                操作指南
              </div>
            </div>
          )}
        </div>
      </div>
      {/* Guide Image Preview */}
      {isOriginY && isOperationGuideContext && (
        <div style={{ display: "none" }}>
          <Image.PreviewGroup
            preview={{
              visible: guidePreviewVisible,
              rootClassName: "chat-sidebar-guide-preview",
              onVisibleChange: (vis) => setGuidePreviewVisible(vis),
            }}
          >
            <Image src={shGuideImage} />
          </Image.PreviewGroup>
        </div>
      )}
    </>
  );
}
