import React, { useState, useCallback, useEffect, useId } from "react";
import type { CronJobSpecOutput } from "@/api/types";
import { taskIdentityKey } from "@/api/modules/cronTaskSources";
import Style from "./style";
import { TasksIconSmall } from "../ChatSidebar/CollapsedToolbar/icons";
import {
  getTaskNextRunText,
  getTaskNextRunTooltipTimes,
  getTaskPauseStatusText,
  getTaskSidebarMeta,
  getTaskLastRunDisplay,
  partitionTasksByPauseState,
} from "../../taskJobs";
import { formatListTime } from "../../listTimeFormat";
import TaskActionMenu from "../TaskActionMenu";
import TaskNextRunTooltip from "../TaskNextRunTooltip";

function ToggleIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg
      width="10"
      height="6"
      viewBox="0 0 10 6"
      fill="none"
      className={`chat-task-list-toggle${
        collapsed ? " chat-task-list-toggle--collapsed" : ""
      }`}
    >
      <path
        d="M1 1L5 5L9 1"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export interface ChatTaskListProps {
  tasks: CronJobSpecOutput[];
  selectedTaskId?: string;
  onTaskClick?: (task: CronJobSpecOutput) => void;
  onTaskPause?: (task: CronJobSpecOutput) => void;
  onTaskRun?: (task: CronJobSpecOutput) => void;
  onTaskResume?: (task: CronJobSpecOutput) => void;
  onTaskDelete?: (task: CronJobSpecOutput) => void;
  onTaskEdit?: (task: CronJobSpecOutput) => void;
}

export default function ChatTaskList(props: ChatTaskListProps) {
  const {
    tasks,
    selectedTaskId,
    onTaskClick,
    onTaskPause,
    onTaskRun,
    onTaskResume,
    onTaskDelete,
    onTaskEdit,
  } = props;
  const [collapsed, setCollapsed] = useState(false);
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

  const handleToggle = useCallback(() => {
    setCollapsed((prev) => !prev);
  }, []);

  const handleTaskClick = useCallback(
    (task: CronJobSpecOutput) => {
      onTaskClick?.(task);
    },
    [onTaskClick],
  );

  const renderTask = (task: CronJobSpecOutput) => {
    const sidebarMeta = getTaskSidebarMeta(task);
    const lastRun = getTaskLastRunDisplay(task);
    const pauseStatusText = getTaskPauseStatusText(sidebarMeta);
    const nextRunText = getTaskNextRunText(task);
    const nextRunTooltipTimes = getTaskNextRunTooltipTimes(task);

    return (
      <div
        key={taskIdentityKey(task)}
        className={`chat-task-list-item${
          taskIdentityKey(task) === selectedTaskId
            ? " chat-task-list-item--selected"
            : ""
        }${
          sidebarMeta.state !== "active" && sidebarMeta.state !== "running"
            ? " chat-task-list-item--paused"
            : ""
        }${
          sidebarMeta.state === "running" ? " chat-task-list-item--running" : ""
        }${
          sidebarMeta.state === "auto-paused"
            ? " chat-task-list-item--auto-paused"
            : ""
        }`}
        onClick={() => handleTaskClick(task)}
        role="button"
        tabIndex={0}
      >
        <div className="chat-task-list-item-header">
          {sidebarMeta.unreadCount > 0 && (
            <span className="chat-task-list-item-badge">
              {sidebarMeta.unreadCount > 99 ? "99+" : sidebarMeta.unreadCount}
            </span>
          )}
          <span className="chat-task-list-item-title">
            {task.name || task.id}
          </span>
          {(sidebarMeta.canPause ||
            sidebarMeta.canRun ||
            sidebarMeta.canResume ||
            sidebarMeta.canDelete ||
            (onTaskEdit && sidebarMeta.canEdit)) && (
            <div className="chat-task-list-item-trailing">
              <div className="chat-task-list-item-actions">
                <TaskActionMenu
                  task={task}
                  sidebarMeta={sidebarMeta}
                  classNamePrefix="chat-task-list-item"
                  onTaskPause={onTaskPause}
                  onTaskRun={onTaskRun}
                  onTaskResume={onTaskResume}
                  onTaskDelete={onTaskDelete}
                  onTaskEdit={onTaskEdit}
                />
              </div>
            </div>
          )}
        </div>

        {pauseStatusText && (
          <div
            className={`chat-task-list-item-status ${
              sidebarMeta.state === "auto-paused"
                ? "chat-task-list-item-status--auto"
                : "chat-task-list-item-status--manual"
            }`}
          >
            {pauseStatusText}
          </div>
        )}
        {lastRun && (
          <div className="chat-task-list-item-subtitle">
            {lastRun.at && (
              <span className="chat-task-list-item-time">
                {formatListTime(lastRun.at)}
              </span>
            )}
            {lastRun.statusText}
          </div>
        )}
        {nextRunText && (
          <TaskNextRunTooltip runTimes={nextRunTooltipTimes}>
            <div className="chat-task-list-item-next-run">{nextRunText}</div>
          </TaskNextRunTooltip>
        )}
      </div>
    );
  };

  return (
    <>
      <Style />
      <div className="chat-task-list">
        <div
          className="chat-task-list-header"
          onClick={handleToggle}
          role="button"
          tabIndex={0}
        >
          <div className="chat-task-list-title">
            <TasksIconSmall />
            我的任务({tasks.length})
          </div>
          <ToggleIcon collapsed={collapsed} />
        </div>
        {!collapsed && (
          <div className="chat-task-list-items">
            {tasks.length === 0 ? (
              <div className="chat-task-list-empty">
                <div className="chat-task-list-empty-title">暂无任务</div>
                <div className="chat-task-list-empty-description">
                  创建任务，让 AI 帮你自动推进
                </div>
              </div>
            ) : (
              <>
                {runnableTasks.map(renderTask)}
                {pausedTasks.length > 0 && (
                  <div className="chat-task-list-paused-group">
                    <button
                      type="button"
                      className="chat-task-list-paused-toggle"
                      aria-label={`已暂停任务 ${pausedTasks.length}`}
                      aria-expanded={!pausedCollapsed}
                      aria-controls={pausedRegionId}
                      onClick={() => setPausedCollapsed((prev) => !prev)}
                    >
                      <ToggleIcon collapsed={pausedCollapsed} />
                      <span className="chat-task-list-paused-label">
                        已暂停任务
                      </span>
                      <span
                        className="chat-task-list-paused-count"
                        aria-hidden="true"
                      >
                        {pausedTasks.length}
                      </span>
                    </button>
                    <div
                      id={pausedRegionId}
                      className="chat-task-list-paused-items"
                      hidden={pausedCollapsed}
                    >
                      {pausedTasks.map(renderTask)}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </>
  );
}
