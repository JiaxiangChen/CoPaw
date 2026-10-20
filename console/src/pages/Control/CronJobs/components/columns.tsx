import { Button, Tooltip, Dropdown } from "@agentscope-ai/design";
import type { ColumnsType } from "antd/es/table";
import type { MenuProps } from "antd";
import { Popover, Tag } from "antd";
import type { CronJobSpecOutput } from "../../../../api/types";
import { CopyOutlined, MoreOutlined } from "@ant-design/icons";
import { TFunction } from "i18next";
import { parseCron, CronType } from "./parseCron";
import { copyToClipboard } from "../../../../utils/clipboard";
import type { ExecutionModelOption } from "@/hooks/useExecutionModelOptions";
import { formatExecutionModelLabel } from "@/hooks/useExecutionModelOptions";
import { formatNotificationDelay } from "@/utils/cron";
import styles from "../index.module.less";

type CronJob = CronJobSpecOutput;

export interface BroadcastParentInfo {
  sourceJobId: string;
  sourceJobName: string;
  sourceTenantId: string;
  sourceTenantName: string;
  sourceBbkId: string;
}

interface ColumnHandlers {
  onToggleEnabled: (job: CronJob) => void;
  onExecuteNow: (job: CronJob) => void;
  onBroadcast: (job: CronJob) => void;
  onManageChildren: (job: CronJob) => void;
  onBatchConfigure: (job: CronJob) => void;
  onEdit: (job: CronJob) => void;
  onDelete: (jobId: string) => void;
  onCopySuccess: () => void;
  onCopyError: () => void;
  executionModelOptions: ExecutionModelOption[];
  tenantDefaultModelLabel: string;
  t: TFunction;
}

function optionalMetaText(value: unknown): string {
  if (typeof value === "string") {
    return value.trim();
  }
  if (value === null || value === undefined) {
    return "";
  }
  return String(value).trim();
}

export function getBroadcastParentInfo(job: CronJob): BroadcastParentInfo {
  const meta = job.meta || {};
  return {
    sourceJobId: optionalMetaText(meta.broadcast_source_job_id),
    sourceJobName: optionalMetaText(meta.broadcast_source_job_name),
    sourceTenantId: optionalMetaText(meta.broadcast_source_tenant_id),
    sourceTenantName: optionalMetaText(meta.broadcast_source_tenant_name),
    sourceBbkId: optionalMetaText(meta.broadcast_source_bbk_id),
  };
}

export function isBroadcastChildJob(job: CronJob): boolean {
  return Boolean(getBroadcastParentInfo(job).sourceJobId);
}

export function isBatchDispatchJob(job: CronJob): boolean {
  return job.meta?.broadcast_dispatch_intents_enabled === true;
}

function formatParentUser(info: BroadcastParentInfo): string {
  if (!info.sourceTenantId && !info.sourceTenantName) {
    return "未记录";
  }
  return info.sourceTenantName
    ? `${info.sourceTenantName}${info.sourceTenantId ? ` (${info.sourceTenantId})` : ""
    }`
    : info.sourceTenantId;
}

function renderBroadcastChildTag(record: CronJob) {
  const parentInfo = getBroadcastParentInfo(record);
  if (!parentInfo.sourceJobId) {
    return null;
  }
  const content = (
    <div className={styles.broadcastChildPopover}>
      <div>
        <span className={styles.broadcastChildPopoverLabel}>父用户</span>
        <span>{formatParentUser(parentInfo)}</span>
      </div>
      <div>
        <span className={styles.broadcastChildPopoverLabel}>父任务</span>
        <span>{parentInfo.sourceJobName || "未记录"}</span>
      </div>
      <div>
        <span className={styles.broadcastChildPopoverLabel}>父任务ID</span>
        <span>{parentInfo.sourceJobId}</span>
      </div>
    </div>
  );

  return (
    <Popover content={content} placement="topLeft" trigger="hover">
      <Tag color="blue" className={styles.broadcastChildTag}>
        分发子任务
      </Tag>
    </Popover>
  );
}

export const createColumns = (
  handlers: ColumnHandlers,
): ColumnsType<CronJob> => {
  const handleCopy = async (text: string) => {
    const success = await copyToClipboard(text);
    if (success) {
      handlers.onCopySuccess();
    } else {
      handlers.onCopyError();
    }
  };

  return [
    {
      title: handlers.t("cronJobs.id"),
      dataIndex: "id",
      key: "id",
      width: 250,
      fixed: "left",
    },
    {
      title: handlers.t("cronJobs.name"),
      dataIndex: "name",
      key: "name",
      width: 250,
      render: (_name: string, record: CronJob) => (
        <div className={styles.jobNameCell}>
          <span className={styles.jobNameText}>{record.name}</span>
          {renderBroadcastChildTag(record)}
        </div>
      ),
    },
    {
      title: handlers.t("cronJobs.enabled"),
      dataIndex: "enabled",
      key: "enabled",
      width: 100,
      render: (enabled: boolean) => (
        <span className={styles.statusIndicator}>
          <span
            className={`${styles.statusDot} ${enabled ? styles.enabled : styles.disabled
              }`}
          />
          {enabled
            ? handlers.t("common.enabled")
            : handlers.t("common.disabled")}
        </span>
      ),
    },
    {
      title: "Status",
      key: "last_status",
      width: 120,
      render: (_: unknown, record: CronJob) => {
        const isRunning = record.task?.is_running;
        const status = isRunning
          ? "running"
          : record.state?.last_status || "idle";
        const toneClass =
          status === "running"
            ? styles.statusDotRunning
            : status === "success"
              ? styles.statusDotSuccess
              : status === "error" || status === "cancelled"
                ? styles.statusDotError
                : styles.statusDotIdle;


        return (
          <span className={styles.statusIndicator}>
            <span className={`${styles.statusDot} ${toneClass}`} />
            {status}
          </span>
        );
      },
    },
    {
      title: handlers.t("cronJobs.scheduleType"),
      dataIndex: ["schedule", "type"],
      key: "schedule_type",
      width: 140,
      render: () => "cron",
    },
    {
      title: handlers.t("cronJobs.scheduleCron"),
      dataIndex: ["schedule", "cron"],
      key: "cron",
      width: 180,
      render: (cron: string, record: CronJob) => {
        const batchDispatch = isBatchDispatchJob(record);


        // Parse cron to friendly text
        const cronParts = parseCron(cron || "0 9 * * *");
        let displayText = "";


        switch (cronParts.type) {
          case CronType.hourly:
            displayText = handlers.t("cronJobs.cronTypeHourly");
            break;
          case CronType.daily:
            displayText = `${handlers.t("cronJobs.cronTypeDaily")} ${String(
              cronParts.hour,
            ).padStart(2, "0")}:${String(cronParts.minute).padStart(2, "0")}`;
            break;
          case CronType.weekly: {
            const dayNames = (cronParts.daysOfWeek || [])
              .map((d) => {
                const dayMap: Record<string, string> = {
                  mon: handlers.t("cronJobs.cronDayMon"),
                  tue: handlers.t("cronJobs.cronDayTue"),
                  wed: handlers.t("cronJobs.cronDayWed"),
                  thu: handlers.t("cronJobs.cronDayThu"),
                  fri: handlers.t("cronJobs.cronDayFri"),
                  sat: handlers.t("cronJobs.cronDaySat"),
                  sun: handlers.t("cronJobs.cronDaySun"),
                };
                return dayMap[d] || d;
              })
              .join(",");
            displayText = `${handlers.t(
              "cronJobs.cronTypeWeekly",
            )} ${dayNames} ${String(cronParts.hour).padStart(2, "0")}:${String(
              cronParts.minute,
            ).padStart(2, "0")}`;
            break;
          }
          case CronType.interval:
            displayText = cronParts.everyMinute || (cronParts.minuteStep ?? 1) === 1
              ? "每分钟"
              : `每 ${cronParts.minuteStep ?? 1} 分钟`;
            break;


          case CronType.monthly:
            displayText = `每月 ${cronParts.day ?? 1} 日 ${String(
              cronParts.hour,
            ).padStart(2, "0")}:${String(cronParts.minute).padStart(2, "0")}`;
            break;
          case CronType.yearly:
            displayText = `每年 ${cronParts.month ?? 1} 月 ${cronParts.day ?? 1} 日 ${String(
              cronParts.hour,
            ).padStart(2, "0")}:${String(cronParts.minute).padStart(2, "0")}`;
            break;
          case CronType.custom:
          default:
            displayText = cron;
            break;
        }

        return (
          <Tooltip
            title={
              <div>
                <div>Cron 表达式：{cron}</div>
                <div
                  className={styles.tableText}
                  style={{ opacity: 0.8, marginTop: 4 }}
                >
                  格式：分钟 小时 日 月 星期
                </div>
              </div>
            }
          >
            <span className={styles.cronText}>
              {batchDispatch && <Tag color="processing">批调度</Tag>}
              <span>{displayText}</span>
            </span>
          </Tooltip>
        );
      },
    },
    {
      title: handlers.t("cronJobs.scheduleTimezone"),
      dataIndex: ["schedule", "timezone"],
      key: "timezone",
      width: 170,
    },
    {
      title: "通知延迟",
      key: "notification_delay",
      width: 170,
      render: (_: unknown, record: CronJob) =>
        formatNotificationDelay(record.meta?.notification_delay_minutes),
    },
    {
      title: "TaskType",
      dataIndex: "task_type",
      key: "task_type",
      width: 140,
      render: (value: string) => (value === "workflow" ? "技能任务" : value),
    },
    {
      title: "ExecutionModel",
      key: "model_slot",
      width: 240,
      render: (_: unknown, record: CronJob) => {
        if (record.task_type !== "agent") {
          return "-";
        }
        return formatExecutionModelLabel(
          record.model_slot,
          handlers.executionModelOptions,
          handlers.tenantDefaultModelLabel,
        );
      },
    },
    {
      title: handlers.t("cronJobs.text"),
      dataIndex: "text",
      key: "text",
      width: 200,
      ellipsis: {
        showTitle: true,
      },
      render: (text: string) => {
        if (!text) return "-";
        return (
          <Tooltip title={text}>
            <span className={styles.tableText}>{text}</span>
          </Tooltip>
        );
      },
    },
    {
      title: handlers.t("cronJobs.requestInput"),
      dataIndex: ["request", "input"],
      key: "request_input",
      width: 350,
      ellipsis: true,
      render: (input: unknown) => {
        if (!input) return "-";

        let displayText: string;
        let fullText: string;

        try {
          fullText = JSON.stringify(input, null, 2);
          displayText = JSON.stringify(input);
        } catch {
          fullText = String(input);
          displayText = fullText;
        }

        if (displayText.length <= 50) {
          return <code className={styles.codeText}>{displayText}</code>;
        }

        const truncatedText =
          displayText.length > 50
            ? displayText.substring(0, 50) + "..."
            : displayText;

        return (
          <Tooltip
            title={
              <div className={styles.tooltipContent}>
                <div className={styles.tooltipJsonContent}>{fullText}</div>
                <Button
                  type="text"
                  icon={<CopyOutlined />}
                  size="small"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleCopy(fullText);
                  }}
                  className={styles.copyButton}
                />
              </div>
            }
            placement="topLeft"
            overlayInnerStyle={{ maxWidth: 400 }}
          >
            <code className={styles.codeLink}>{truncatedText}</code>
          </Tooltip>
        );
      },
    },
    {
      title: "RequestSessionID",
      dataIndex: ["request", "session_id"],
      key: "session_id",
      width: 160,
    },
    {
      title: "RequestUserID",
      dataIndex: ["request", "user_id"],
      key: "user_id",
      width: 140,
    },
    {
      title: "DispatchType",
      dataIndex: ["dispatch", "type"],
      key: "dispatch_type",
      width: 140,
    },
    {
      title: "DispatchChannel",
      dataIndex: ["dispatch", "channel"],
      key: "channel",
      width: 150,
    },
    {
      title: "DispatchTargetUserID",
      dataIndex: ["dispatch", "target", "user_id"],
      key: "target_user_id",
      width: 190,
    },
    {
      title: "DispatchTargetSessionID",
      dataIndex: ["dispatch", "target", "session_id"],
      key: "target_session_id",
      width: 210,
    },
    {
      title: "DispatchMode",
      dataIndex: ["dispatch", "mode"],
      key: "mode",
      width: 140,
    },
    {
      title: "RuntimeMaxConcurrency",
      dataIndex: ["runtime", "max_concurrency"],
      key: "max_concurrency",
      width: 210,
    },
    {
      title: "RuntimeTimeoutSeconds",
      dataIndex: ["runtime", "timeout_seconds"],
      key: "timeout_seconds",
      width: 210,
    },
    {
      title: "RuntimeMisfireGraceSeconds",
      dataIndex: ["runtime", "misfire_grace_seconds"],
      key: "misfire_grace_seconds",
      width: 240,
    },
    {
      title: handlers.t("cronJobs.action"),
      key: "action",
      width: 240,
      fixed: "right",
      render: (_: unknown, record: CronJob) => {
        const broadcastChild = isBroadcastChildJob(record);
        const batchConfigurationDisabled =
          broadcastChild || !isBatchDispatchJob(record);
        const menuItems: MenuProps["items"] = [
          {
            key: "broadcast",
            label: broadcastChild ? "广播到租户（子任务不支持）" : "广播到租户",
            disabled: broadcastChild,
            onClick: () => {
              if (!broadcastChild) {
                handlers.onBroadcast(record);
              }
            },
          },
          {
            key: "broadcast_children",
            label: broadcastChild
              ? "查看分发用户（子任务不支持）"
              : "查看分发用户",
            disabled: broadcastChild,
            onClick: () => {
              if (!broadcastChild) {
                handlers.onManageChildren(record);
              }
            },
          },
          {
            key: "batch_configuration",
            label: "批调度配置",
            disabled: batchConfigurationDisabled,
            onClick: () => {
              if (!batchConfigurationDisabled) {
                handlers.onBatchConfigure(record);
              }
            },
          },
          {
            key: "edit",
            label: handlers.t("cronJobs.edit"),
            disabled: record.enabled,
            onClick: () => handlers.onEdit(record),
          },
          {
            key: "delete",
            label: handlers.t("cronJobs.delete"),
            disabled: record.enabled,
            danger: true,
            onClick: () => handlers.onDelete(record.id),
          },
        ];

        return (
          <div className={styles.actionColumn}>
            <Button
              type="link"
              size="small"
              onClick={() => handlers.onToggleEnabled(record)}
            >
              {record.enabled
                ? handlers.t("cronJobs.disable")
                : handlers.t("common.enable")}
            </Button>
            <Button
              type="link"
              size="small"
              onClick={() => handlers.onExecuteNow(record)}
            >
              {handlers.t("cronJobs.executeNow")}
            </Button>
            <Dropdown menu={{ items: menuItems }} placement="bottomRight">
              <Button type="text" size="small" icon={<MoreOutlined />} />
            </Dropdown>
          </div>
        );
      },
    },
  ];
};