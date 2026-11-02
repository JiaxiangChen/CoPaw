import { useCallback } from "react";
import { Alert, Descriptions, Skeleton, Tag } from "antd";
import { cronTaskTracking } from "../../../api/modules/cronTaskTracking";
import type {
  TaskIdentity,
  TrackingMetadata,
} from "../../../api/types/cronTaskTracking";
import { useTrackingResource } from "./useTrackingResource";
import { color, statuses, text, time } from "./format";
import styles from "./index.module.less";

export function Coverage({ value }: { value?: TrackingMetadata }) {
  if (!value?.warnings.length) return null;
  return (
    <Alert
      type="warning"
      showIcon
      message="部分数据缺失或不可用，已展示其余可验证记录"
      description={
        <span className={styles.wrap}>
          {value.warnings.join(" · ")}（查询 ID：{value.query_id}）
        </span>
      }
    />
  );
}

export default function TrackingOverview({
  source,
  identity,
  revision,
}: {
  source: string;
  identity: TaskIdentity;
  revision: number;
}) {
  const load = useCallback(
    (signal: AbortSignal) =>
      cronTaskTracking.overview(source, identity, signal),
    [source, identity],
  );
  const resource = useTrackingResource(load, revision);
  if (resource.loading)
    return <Skeleton active paragraph={{ rows: 3 }} title={false} />;
  if (resource.error)
    return (
      <Alert
        type="error"
        showIcon
        message="当前属性加载失败"
        description={resource.error}
      />
    );
  const value = resource.value!;
  const definition = value.definition || {};
  const state = value.state || {};
  const relation = {
    parent: "父任务",
    child: "子任务",
    ordinary: "普通任务",
    unknown: "归属未确认",
  }[value.relation];
  const fields: [string, unknown][] = [
    ["任务类型", definition.task_type],
    ["用户名称", definition.tenant_name],
    [
      "用户 / 来源 / Agent",
      `${identity.tenant_id} / ${identity.source_id} / ${identity.agent_id}`,
    ],
    [
      "当前排程 / 时区",
      `${text(definition.cron_expr)} / ${text(definition.timezone)}`,
    ],
    ["原始排程", definition.original_cron_expr],
    [
      "可执行时间区间",
      definition.run_start_date && definition.run_end_date
        ? `${text(definition.run_start_date)} 至 ${text(
            definition.run_end_date,
          )}`
        : value.definition
        ? "不限日期段"
        : undefined,
    ],
    [
      "日期暂停（保存标记）",
      state.date_window_paused === true || state.date_window_paused === 1
        ? "日期暂停"
        : state.date_window_paused === false || state.date_window_paused === 0
        ? "未标记日期暂停"
        : undefined,
    ],
    [
      "日期平台同步标记",
      state.date_window_sync_required === true ||
      state.date_window_sync_required === 1
        ? "待同步"
        : state.date_window_sync_required === false ||
          state.date_window_sync_required === 0
        ? "无待同步标记"
        : undefined,
    ],
    [
      "启用",
      definition.enabled === true || definition.enabled === 1
        ? "已启用"
        : definition.enabled === false || definition.enabled === 0
        ? "已停用"
        : undefined,
    ],
    [
      "调度方式",
      statuses[String(state.dispatch_state)] || state.dispatch_state,
    ],
    [
      "管理状态",
      statuses[String(state.management_status)] || state.management_status,
    ],
    ["暂停原因", state.pause_reason],
    ["下次计划（保存值）", time(state.next_run_at)],
    ["普通定时器 ID", state.ordinary_external_job_id],
    ["批调度定时器 ID", state.batch_external_job_id],
    ["定义版本", definition.definition_version],
    ["未读次数", state.unread_execution_count],
  ];
  return (
    <section aria-label="任务当前概况" className={styles.overview}>
      <div className={styles.sectionTitle}>
        <h2>{text(definition.name || identity.job_id)}</h2>
        <Tag>{relation}</Tag>
        <Tag color={color(state.last_status)}>
          {statuses[String(state.last_status)] || text(state.last_status)}
        </Tag>
        {definition.deleted_at ? <Tag color="warning">已逻辑删除</Tag> : null}
      </div>
      <p className={styles.muted}>
        当前快照 · {time(value.queried_at)} · 不随时间轴筛选回放历史属性
      </p>
      {value.history_only ? (
        <Alert type="info" message="历史任务，当前定义不可用" />
      ) : null}
      <Coverage value={value} />
      <Descriptions
        size="small"
        column={{ xs: 1, sm: 2, lg: 3 }}
        items={fields.map(([label, value]) => ({
          key: label as string,
          label,
          children: <span className={styles.wrap}>{text(value)}</span>,
        }))}
      />
      <details className={styles.attributeDetails}>
        <summary>查看完整安全属性与当前状态</summary>
        <pre>
          {JSON.stringify(
            { definition: value.definition, state: value.state },
            null,
            2,
          )}
        </pre>
      </details>
    </section>
  );
}
