import React, { useEffect, useRef, useCallback, useId, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { CronJobSpecOutput } from "@/api/types";
import { taskIdentityKey } from "@/api/modules/cronTaskSources";
import type { IAgentScopeRuntimeWebUISession } from "@/components/agentscope-chat";
import { useChatAnywhereSessionsState } from "@/components/agentscope-chat";
import { TasksIconSmall, HistoryIconSmall } from "../CollapsedToolbar/icons";
import Style from "./style";
import {
  getTaskNextRunText,
  getTaskNextRunTooltipTimes,
  getTaskPauseStatusText,
  getTaskSidebarMeta,
  getTaskLastRunDisplay,
  partitionTasksByPauseState,
} from "../../../taskJobs";
import { formatListTime } from "../../../listTimeFormat";
import {
  getHistorySessionTargetId,
  isHistorySessionActive,
  type HistorySession,
} from "../historySessions";
import TaskActionMenu from "../../TaskActionMenu";
import TaskNextRunTooltip from "../../TaskNextRunTooltip";
import { HistoryInfiniteScrollTrigger } from "../HistoryInfiniteScrollTrigger";

export interface ExpandablePanelProps {
  visible: boolean;
  type: "tasks" | "history";
  onClose: () => void;
  tasks: CronJobSpecOutput[];
  selectedTaskId?: string;
  sessions: IAgentScopeRuntimeWebUISession[];
  onTaskClick: (task: CronJobSpecOutput) => void;
  onTaskPause?: (task: CronJobSpecOutput) => void;
  onTaskRun?: (task: CronJobSpecOutput) => void;
  onTaskResume?: (task: CronJobSpecOutput) => void;
  onTaskDelete?: (task: CronJobSpecOutput) => void;
  onTaskEdit?: (task: CronJobSpecOutput) => void;
  toolbarRef: React.RefObject<HTMLElement | null>;
  hasMoreSessions?: boolean;
  sessionTotal?: number;
  isLoadingMoreSessions?: boolean;
  loadMoreSessionsFailed?: boolean;
  onLoadMoreSessions?: () => void;
}

export default function ExpandablePanel({
  visible,
  type,
  onClose,
  tasks,
  selectedTaskId,
  sessions,
  onTaskClick,
  onTaskPause,
  onTaskRun,
  onTaskResume,
  onTaskDelete,
  onTaskEdit,
  toolbarRef,
  hasMoreSessions = false,
  sessionTotal,
  isLoadingMoreSessions = false,
  loadMoreSessionsFailed = false,
  onLoadMoreSessions,
}: ExpandablePanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!visible) return;

    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (
        panelRef.current &&
        !panelRef.current.contains(target) &&
        toolbarRef.current &&
        !toolbarRef.current.contains(target)
      ) {
        onClose();
      }
    };

    const timer = setTimeout(() => {
      document.addEventListener("mousedown", handleClickOutside);
    }, 0);

    return () => {
      clearTimeout(timer);
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, [visible, onClose, toolbarRef]);

  useEffect(() => {
    if (!visible) return;
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleEscape);
    return () => document.removeEventListener("keydown", handleEscape);
  }, [visible, onClose]);

  if (!visible) return null;

  return (
    <>
      <Style />
      <div className="expandable-panel" ref={panelRef}>
        {type === "tasks" ? (
          <TasksContent
            tasks={tasks}
            selectedTaskId={selectedTaskId}
            onTaskClick={onTaskClick}
            onTaskPause={onTaskPause}
            onTaskRun={onTaskRun}
            onTaskResume={onTaskResume}
            onTaskDelete={onTaskDelete}
            onTaskEdit={onTaskEdit}
          />
        ) : (
          <HistoryContent
            sessions={sessions}
            onClose={onClose}
            hasMoreSessions={hasMoreSessions}
            sessionTotal={sessionTotal ?? sessions.length}
            isLoadingMoreSessions={isLoadingMoreSessions}
            loadMoreSessionsFailed={loadMoreSessionsFailed}
            onLoadMoreSessions={onLoadMoreSessions}
          />
        )}
      </div>
    </>
  );
}

function TasksContent({
  tasks,
  selectedTaskId,
  onTaskClick,
  onTaskPause,
  onTaskRun,
  onTaskResume,
  onTaskDelete,
  onTaskEdit,
}: {
  tasks: CronJobSpecOutput[];
  selectedTaskId?: string;
  onTaskClick: (task: CronJobSpecOutput) => void;
  onTaskPause?: (task: CronJobSpecOutput) => void;
  onTaskRun?: (task: CronJobSpecOutput) => void;
  onTaskResume?: (task: CronJobSpecOutput) => void;
  onTaskDelete?: (task: CronJobSpecOutput) => void;
  onTaskEdit?: (task: CronJobSpecOutput) => void;
}) {
  const [pausedCollapsed, setPausedCollapsed] = useState(true);
  const pausedRegionId = useId();
  const { runnableTasks, pausedTasks } = partitionTasksByPauseState(tasks);
  const selectedTaskIsPaused = pausedTasks.some(
    (task) => taskIdentityKey(task) === selectedTaskId,
  );

  useEffect(() => {
    if (selectedTaskIsPaused) {
      setPausedCollapsed(false);
    }
  }, [selectedTaskId, selectedTaskIsPaused]);

  const renderTask = (task: CronJobSpecOutput) => {
    const sidebarMeta = getTaskSidebarMeta(task);
    const lastRun = getTaskLastRunDisplay(task);
    const pauseStatusText = getTaskPauseStatusText(sidebarMeta);
    const nextRunText = getTaskNextRunText(task);
    const nextRunTooltipTimes = getTaskNextRunTooltipTimes(task);

    return (
      <div
        key={taskIdentityKey(task)}
        className={`expandable-panel-task-card${
          taskIdentityKey(task) === selectedTaskId
            ? " expandable-panel-task-card--selected"
            : ""
        }${
          sidebarMeta.state !== "active" && sidebarMeta.state !== "running"
            ? " expandable-panel-task-card--paused"
            : ""
        }${
          sidebarMeta.state === "auto-paused"
            ? " expandable-panel-task-card--auto-paused"
            : ""
        }${
          sidebarMeta.state === "running"
            ? " expandable-panel-task-card--running"
            : ""
        }`}
        onClick={() => onTaskClick(task)}
        role="button"
        tabIndex={0}
      >
        <div className="expandable-panel-task-title-row">
          <span className="expandable-panel-task-title">
            {task.name || task.id}
          </span>
          {(sidebarMeta.unreadCount > 0 ||
            sidebarMeta.canPause ||
            sidebarMeta.canRun ||
            sidebarMeta.canResume ||
            sidebarMeta.canDelete ||
            (onTaskEdit && sidebarMeta.canEdit)) && (
            <div className="expandable-panel-task-trailing">
              {(sidebarMeta.canPause ||
                sidebarMeta.canRun ||
                sidebarMeta.canResume ||
                sidebarMeta.canDelete ||
                (onTaskEdit && sidebarMeta.canEdit)) && (
                <div className="expandable-panel-task-actions">
                  <TaskActionMenu
                    task={task}
                    sidebarMeta={sidebarMeta}
                    classNamePrefix="expandable-panel-task"
                    onTaskPause={onTaskPause}
                    onTaskRun={onTaskRun}
                    onTaskResume={onTaskResume}
                    onTaskDelete={onTaskDelete}
                    onTaskEdit={onTaskEdit}
                  />
                </div>
              )}
              {sidebarMeta.unreadCount > 0 && (
                <span className="expandable-panel-task-badge">
                  {sidebarMeta.unreadCount > 99
                    ? "99+"
                    : sidebarMeta.unreadCount}
                </span>
              )}
            </div>
          )}
        </div>
        {pauseStatusText && (
          <div
            className={`expandable-panel-task-status ${
              sidebarMeta.state === "auto-paused"
                ? "expandable-panel-task-status--auto"
                : "expandable-panel-task-status--manual"
            }`}
          >
            {pauseStatusText}
          </div>
        )}
        {lastRun && (
          <div className="expandable-panel-task-subtitle">
            {lastRun.at && (
              <span className="expandable-panel-task-time">
                {formatListTime(lastRun.at)}
              </span>
            )}
            {lastRun.statusText}
          </div>
        )}
        {nextRunText && (
          <TaskNextRunTooltip runTimes={nextRunTooltipTimes}>
            <div className="expandable-panel-task-next-run">{nextRunText}</div>
          </TaskNextRunTooltip>
        )}
      </div>
    );
  };

  return (
    <>
      <div className="expandable-panel-header">
        <TasksIconSmall />
        <span className="expandable-panel-header-title">
          我的任务({tasks.length})
        </span>
      </div>
      <div className="expandable-panel-content">
        {tasks.length === 0 ? (
          <div className="expandable-panel-task-empty">
            <div className="expandable-panel-task-empty-title">暂无任务</div>
            <div className="expandable-panel-task-empty-description">
              创建任务，让 AI 帮你自动推进
            </div>
          </div>
        ) : (
          <>
            {pausedTasks.length > 0 && (
              <div className="expandable-panel-paused-group">
                <button
                  type="button"
                  className="expandable-panel-paused-toggle"
                  aria-label={`已暂停任务 ${pausedTasks.length}`}
                  aria-expanded={!pausedCollapsed}
                  aria-controls={pausedRegionId}
                  onClick={() => setPausedCollapsed((prev) => !prev)}
                >
                  <span
                    className={`expandable-panel-paused-chevron${
                      pausedCollapsed
                        ? " expandable-panel-paused-chevron--collapsed"
                        : ""
                    }`}
                    aria-hidden="true"
                  >
                    <svg width="10" height="6" viewBox="0 0 10 6" fill="none">
                      <path
                        d="M1 1L5 5L9 1"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  </span>
                  <span className="expandable-panel-paused-label">
                    已暂停任务
                  </span>
                  <span
                    className="expandable-panel-paused-count"
                    aria-hidden="true"
                  >
                    {pausedTasks.length}
                  </span>
                </button>
                <div
                  id={pausedRegionId}
                  className="expandable-panel-paused-items"
                  hidden={pausedCollapsed}
                >
                  {pausedTasks.map(renderTask)}
                </div>
              </div>
            )}
            {runnableTasks.map(renderTask)}
          </>
        )}
      </div>
    </>
  );
}

function HistoryContent({
  sessions,
  onClose,
  hasMoreSessions,
  sessionTotal,
  isLoadingMoreSessions,
  loadMoreSessionsFailed,
  onLoadMoreSessions,
}: {
  sessions: IAgentScopeRuntimeWebUISession[];
  onClose: () => void;
  hasMoreSessions: boolean;
  sessionTotal: number;
  isLoadingMoreSessions: boolean;
  loadMoreSessionsFailed: boolean;
  onLoadMoreSessions?: () => void;
}) {
  const navigate = useNavigate();
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const { currentSessionId, setSessionLoading } =
    useChatAnywhereSessionsState();

  const handleSessionClick = useCallback(
    (session: IAgentScopeRuntimeWebUISession) => {
      if (isHistorySessionActive(session as HistorySession, currentSessionId)) {
        return;
      }

      const targetSessionId = getHistorySessionTargetId(
        session as HistorySession,
      );
      if (!targetSessionId) return;

      // 先设置 loading 状态，避免导航后闪现欢迎页
      setSessionLoading(true);
      navigate(`/chat/${targetSessionId}`, { replace: true });
      onClose();
    },
    [currentSessionId, navigate, onClose, setSessionLoading],
  );

  return (
    <>
      <div className="expandable-panel-header">
        <HistoryIconSmall />
        <span className="expandable-panel-header-title">
          历史记录({sessionTotal})
        </span>
      </div>
      <div className="expandable-panel-content" ref={scrollContainerRef}>
        {sessions.length === 0 && (
          <div className="expandable-panel-empty">暂无历史记录</div>
        )}
        {sessions.map((session) => (
          <div
            key={session.id}
            className="expandable-panel-history-item"
            onClick={() => handleSessionClick(session)}
            role="button"
            tabIndex={0}
          >
            <div className="expandable-panel-history-title">
              {session.name || "新会话"}
            </div>
            <div className="expandable-panel-history-time">
              {formatListTime((session as HistorySession).createdAt)}
            </div>
          </div>
        ))}
        <HistoryInfiniteScrollTrigger
          scrollContainerRef={scrollContainerRef}
          hasMore={hasMoreSessions}
          loading={isLoadingMoreSessions}
          failed={loadMoreSessionsFailed}
          onLoadMore={() => onLoadMoreSessions?.()}
        />
      </div>
    </>
  );
}
