import { Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Button, Dropdown, Modal } from "@agentscope-ai/design";
import { MoreOutlined } from "@ant-design/icons";
import type { TFunction } from "i18next";
import type { ReactNode } from "react";
import type { CronJobSpecOutput } from "@/api/types";
import type { ManagedJob } from "@/api/types/schedulerCron";
import type { ExecutionModelOption } from "@/hooks/useExecutionModelOptions";
import {
  createColumns,
  isBroadcastChildJob,
} from "../CronJobs/components/columns";
import { isBatchDispatchMode, MODE_LABELS, scheduleLabel } from "./helpers";
import styles from "../CronJobs/index.module.less";

export interface ManagedJobPanel {
  kind: "edit" | "broadcast" | "children" | "mode" | "progress";
  job: ManagedJob;
}
type Panel = ManagedJobPanel;
interface ColumnOptions {
  t: TFunction;
  models: { options: ExecutionModelOption[]; tenantDefaultLabel: string };
  message: {
    success: (text: string) => unknown;
    error: (text: string) => unknown;
  };
  perform: (
    kind: "toggle" | "remove" | "run",
    job: ManagedJob,
  ) => Promise<void>;
  setPanel: (panel: ManagedJobPanel) => void;
  busy: boolean;
}

/** 原样式展示列加新协议动作，不复用任何旧管理请求。 */
export function createManagedColumns({
  t,
  models,
  message,
  perform,
  setPanel,
  busy,
}: ColumnOptions): ColumnsType<ManagedJob> {
  const select = (
    job: CronJobSpecOutput,
    kind: Exclude<NonNullable<Panel>["kind"], "create">,
  ) => setPanel({ kind, job: job as ManagedJob });
  const original = createColumns({
    t,
    executionModelOptions: models.options,
    tenantDefaultModelLabel: models.tenantDefaultLabel,
    onToggleEnabled: (job) => void perform("toggle", job as ManagedJob),
    onExecuteNow: (job) => void perform("run", job as ManagedJob),
    onBroadcast: (job) => select(job, "broadcast"),
    onManageChildren: (job) => select(job, "children"),
    onBatchConfigure: (job) => select(job, "mode"),
    onEdit: (job) => select(job, "edit"),
    onDelete: () => {},
    onCopySuccess: () => message.success(t("common.copied")),
    onCopyError: () => message.error(t("common.copyFailed")),
  });
  // 只复用纯展示列；旧版动作列以及旧批开关不进入新控制器。
  const display = original
    .filter((column) => !["action", "enabled"].includes(String(column.key)))
    .map((column) =>
      column.key === "cron"
        ? {
            ...column,
            render: (cron: string, job: ManagedJob) => (
              <Tooltip title={`Cron 表达式：${cron}`}>
                <span className={styles.cronText}>
                  {isBatchDispatchMode(job.dispatch_state) && (
                    <Tag color="processing">批调度</Tag>
                  )}
                  <span>{scheduleLabel(cron, t)}</span>
                </span>
              </Tooltip>
            ),
          }
        : column.key === "last_status"
        ? {
            ...column,
            render: (value: unknown, job: ManagedJob, index: number) => {
              const status = column.render?.(value, job, index) as ReactNode;
              if (job.task?.is_running || job.state?.last_status !== "error")
                return status;
              return (
                <Tooltip
                  title={
                    <span
                      style={{
                        whiteSpace: "pre-wrap",
                        overflowWrap: "anywhere",
                      }}
                    >
                      {job.state.last_error?.trim() ||
                        "暂无失败详情，请查看执行日志"}
                    </span>
                  }
                >
                  <span>{status}</span>
                </Tooltip>
              );
            },
          }
        : column,
    ) as ColumnsType<ManagedJob>;
  display.splice(
    2,
    0,
    {
      title: "启用状态",
      key: "enabled",
      width: 110,
      render: (_, job) => (
        <span className={styles.statusIndicator}>
          <span
            className={`${styles.statusDot} ${
              job.enabled && !job.task?.is_paused && !job.deleted
                ? styles.enabled
                : styles.disabled
            }`}
          />
          {job.deleted
            ? "已删除"
            : job.enabled && !job.task?.is_paused
            ? "已启用"
            : "已停用"}
        </span>
      ),
    },
    {
      title: "调度方式",
      key: "dispatch_state",
      width: 140,
      render: (_, job) => MODE_LABELS[job.dispatch_state],
    },
    {
      title: "管理状态",
      key: "management_status",
      width: 145,
      render: (_, job) => (
        <Button type="link" onClick={() => setPanel({ kind: "progress", job })}>
          {job.management_status}
        </Button>
      ),
    },
  );
  display.push({
    title: t("common.actions"),
    key: "action",
    fixed: "right",
    width: 210,
    render: (_, job) => (
      <div className={styles.actionColumn}>
        <Button
          type="link"
          size="small"
          disabled={busy || job.deleted}
          onClick={() => void perform("toggle", job)}
        >
          {job.enabled && !job.task?.is_paused ? "停用" : "启用"}
        </Button>
        <Button
          type="link"
          size="small"
          disabled={
            busy ||
            job.deleted ||
            !job.enabled ||
            Boolean(job.task?.is_running || job.task?.is_paused)
          }
          onClick={() =>
            Modal.confirm({
              title: t("cronJobs.executeNowTitle"),
              content: t("cronJobs.executeNowContent", { name: job.name }),
              okText: t("cronJobs.executeNowConfirm"),
              okType: "primary",
              cancelText: t("cronJobs.cancelText"),
              onOk: () => perform("run", job),
            })
          }
        >
          立即执行
        </Button>
        <Dropdown
          menu={{
            items: [
              {
                key: "edit",
                label: "编辑",
                disabled: job.deleted || busy,
                onClick: () => setPanel({ kind: "edit", job }),
              },
              {
                key: "broadcast",
                label: "分发",
                disabled: job.deleted || isBroadcastChildJob(job),
                onClick: () => setPanel({ kind: "broadcast", job }),
              },
              {
                key: "children",
                label: "子任务管理",
                disabled: isBroadcastChildJob(job),
                onClick: () => setPanel({ kind: "children", job }),
              },
              {
                key: "mode",
                label: "调度方式 / 批调度配置",
                disabled: job.deleted || isBroadcastChildJob(job),
                onClick: () => setPanel({ kind: "mode", job }),
              },
              {
                key: "progress",
                label: "操作进度",
                onClick: () => setPanel({ kind: "progress", job }),
              },
              {
                key: "delete",
                label: "删除",
                danger: true,
                disabled: job.deleted || busy,
                onClick: () =>
                  Modal.confirm({
                    title: `删除任务「${job.name}」？`,
                    content:
                      "先逻辑删除，已入队但未开始的执行将跳过，在途执行继续。",
                    onOk: () => perform("remove", job),
                  }),
              },
            ],
          }}
          placement="bottomRight"
          trigger={["click"]}
        >
          <Button
            type="text"
            size="small"
            aria-label={`更多操作 ${job.name}`}
            icon={<MoreOutlined />}
          />
        </Dropdown>
      </div>
    ),
  });
  return display;
}
