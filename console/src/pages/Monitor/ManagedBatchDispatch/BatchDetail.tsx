import { useCallback, useState } from "react";
import {
  Alert,
  Input,
  Pagination,
  Select,
  Spin,
  Table,
  Tabs,
  Tooltip,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  managedBatchBoard,
  type ManagedBatch,
  type ManagedBatchMember,
  type ManagedRetryResult,
} from "../../../api/modules/managedBatchBoard";
import styles from "../CronBatchDispatch/index.module.less";
import { labels, statusTag, timeText } from "./format";
import { useBoardResource } from "./useBoardResource";
import FailurePanel from "./FailurePanel";
import PriorityOrder from "./PriorityOrder";
import { getBbkDisplayName } from "../../../constants/bbk";

const memberColumns: ColumnsType<ManagedBatchMember> = [
  {
    title: "顺位",
    dataIndex: "member_order",
    width: 60,
    render: (_, item) => <PriorityOrder item={item} />,
  },
  {
    title: "角色 / 用户",
    width: 145,
    render: (_, item) => (
      <div className={styles.stackCell}>
        <strong>
          {item.member_role === "parent" ? "父任务" : "子任务"} ·{" "}
          {item.tenant_id}
        </strong>
        <span>
          {item.bbk_id ? getBbkDisplayName(item.bbk_id) : "未记录分行"}
        </span>
      </div>
    ),
  },
  {
    title: "任务 / 执行",
    width: 220,
    render: (_, item) => (
      <div className={styles.stackCell}>
        <strong>{item.job_name || item.job_id}</strong>
        <Tooltip title={`定时任务 ID：${item.job_id}`}>
          <span>{item.job_id}</span>
        </Tooltip>
        <span>重试次数：{Math.max(0, item.business_attempt - 1)}</span>
      </div>
    ),
  },
  {
    title: "状态 / 子任务",
    width: 160,
    render: (_, item) => (
      <>
        {statusTag(item.status)}
        <div>
          {item.settled_status
            ? `子任务状态：${item.settled_status}`
            : item.status === "success"
            ? "等待子任务完成"
            : "子任务状态未确认"}
        </div>
      </>
    ),
  },
  {
    title: "模型 / 热度",
    width: 180,
    render: (_, item) => (
      <div className={styles.stackCell}>
        <strong title={`${item.provider_id || "-"} / ${item.model_id || "-"}`}>
          {item.provider_id || "-"} / {item.model_id || "-"}
        </strong>
        <span>热度 {item.viewer_heat_score ?? "未记录"}</span>
      </div>
    ),
  },
  {
    title: "时间",
    width: 245,
    render: (_, item) => (
      <div className={styles.stackCell}>
        <span>最早 {timeText(item.not_before_at)}</span>
        <span>开始 {timeText(item.started_at)}</span>
        <span>完成 {timeText(item.finished_at)}</span>
      </div>
    ),
  },
  {
    title: "结果 / 错误",
    width: 200,
    render: (_, item) => {
      const failed =
        ["error", "dispatch_failed"].includes(item.status) ||
        item.settled_status === "error";
      const message = failed
        ? item.error_message ||
          item.settled_error_code ||
          item.error_code ||
          "-"
        : "-";
      return (
        <Tooltip title={message}>
          <span className={styles.errorText}>{message}</span>
        </Tooltip>
      );
    },
  },
];

export default function BatchDetail({
  source,
  actor,
  batch,
  refresh,
  onQueued,
}: {
  source: string;
  actor: string;
  batch: ManagedBatch;
  refresh: number;
  onQueued: (result: ManagedRetryResult) => void;
}) {
  const [filters, setFilters] = useState({
    member_page: 1,
    member_limit: 50,
    member_status: "",
    member_role: "",
    member_query: "",
    event_page: 1,
    event_limit: 50,
    event_type: "",
  });
  const load = useCallback(
    (signal: AbortSignal) => {
      void refresh;
      return managedBatchBoard.detail(source, batch.batch_id, filters, signal);
    },
    [source, batch.batch_id, filters, refresh],
  );
  const detail = useBoardResource(load);
  const data = detail.value;
  const current = data?.batch || batch;
  return (
    <div className={styles.detailContent}>
      <div className={styles.detailHead}>
        <div>
          <h2>{batch.job_name || "未命名定时任务"}</h2>
          <p>
            <Tooltip title={batch.batch_id}>
              <span className={styles.mono}>{batch.batch_id}</span>
            </Tooltip>
            <span title="父任务本次执行的冻结模型；各成员模型见成员列表">
              {current.provider_id || "-"} / {current.model_id || "-"}
            </span>
          </p>
        </div>
        {statusTag(data?.batch.status || batch.status)}
      </div>
      <div
        className={styles.detailMeta}
        style={{ gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}
      >
        <span>父任务 {batch.job_id}</span>
        <span>计划 {timeText(batch.logical_fire_at)}</span>
        <span>回调 {timeText(batch.created_at)}</span>
      </div>
      {detail.error && <Alert type="error" showIcon message={detail.error} />}
      <Tabs
        defaultActiveKey="members"
        className={styles.detailTabs}
        items={[
          {
            key: "failures",
            label: "失败类型与重试",
            children: (
              <FailurePanel
                source={source}
                actor={actor}
                batchId={batch.batch_id}
                onQueued={onQueued}
              />
            ),
          },
          {
            key: "members",
            label: `执行成员 (${data?.member_total ?? batch.member_count})`,
            children: (
              <div className={styles.intentTab}>
                <div className={styles.intentFilters}>
                  <Input
                    aria-label="筛选新版执行成员"
                    placeholder="筛选用户、任务或名称"
                    value={filters.member_query}
                    onChange={(event) =>
                      setFilters({
                        ...filters,
                        member_query: event.target.value,
                        member_page: 1,
                      })
                    }
                  />
                  <Select
                    aria-label="新版成员角色"
                    value={filters.member_role}
                    options={[
                      { value: "", label: "全部角色" },
                      { value: "parent", label: "父任务" },
                      { value: "child", label: "子任务" },
                    ]}
                    onChange={(value) =>
                      setFilters({
                        ...filters,
                        member_role: value,
                        member_page: 1,
                      })
                    }
                  />
                  <Select
                    aria-label="新版成员状态"
                    value={filters.member_status}
                    options={[
                      { value: "", label: "全部状态" },
                      ...[
                        "queued",
                        "dispatching",
                        "accepted",
                        "running",
                        "success",
                        "error",
                        "dispatch_failed",
                        "skipped",
                        "cancelled",
                      ].map((value) => ({ value, label: labels[value] })),
                    ]}
                    onChange={(value) =>
                      setFilters({
                        ...filters,
                        member_status: value,
                        member_page: 1,
                      })
                    }
                  />
                  <strong>{data?.member_total ?? "-"} 条匹配结果</strong>
                </div>
                <Table
                  loading={detail.loading}
                  size="small"
                  rowKey="member_id"
                  columns={memberColumns}
                  dataSource={data?.members || []}
                  pagination={false}
                  scroll={{ x: 1410, y: 292 }}
                  expandable={{
                    expandedRowRender: (item) => (
                      <div>
                        任务版本 {item.definition_version} / 当前版本{" "}
                        {item.current_definition_version}；操作者{" "}
                        {item.actor_id}；管理状态 {item.management_status}
                        ；调度模式 {item.dispatch_state}；受理{" "}
                        {timeText(item.accepted_at)}；子任务完成{" "}
                        {timeText(item.settled_at)}
                      </div>
                    ),
                  }}
                />
                <nav
                  aria-label="新版成员分页"
                  className={styles.detailPagination}
                >
                  <Pagination
                    current={filters.member_page}
                    pageSize={50}
                    total={data?.member_total || 0}
                    showSizeChanger={false}
                    size="small"
                    onChange={(value) =>
                      setFilters({ ...filters, member_page: value })
                    }
                  />
                </nav>
              </div>
            ),
          },
          {
            key: "events",
            label: `调度事件 (${data?.event_total ?? "-"})`,
            children: (
              <>
                <Input
                  aria-label="筛选新版事件类型"
                  placeholder="事件类型，如 running、retry_accepted"
                  value={filters.event_type}
                  onChange={(event) =>
                    setFilters({
                      ...filters,
                      event_type: event.target.value,
                      event_page: 1,
                    })
                  }
                />
                <Spin spinning={detail.loading}>
                  <div className={styles.eventList}>
                    {data?.events.map((event) => (
                      <div className={styles.eventItem} key={event.event_id}>
                        <time>{timeText(event.occurred_at)}</time>
                        <strong>{event.event_type}</strong>
                        <Tooltip title={event.execution_id}>
                          <p>
                            执行 {event.execution_id || "-"} · 操作者{" "}
                            {event.actor_id}
                          </p>
                        </Tooltip>
                        <em>{JSON.stringify(event.details)}</em>
                      </div>
                    ))}
                  </div>
                </Spin>
                <small>仅展示实际记录事件；升级前缺失的事件不会补造。</small>
                <nav
                  aria-label="新版事件分页"
                  className={styles.detailPagination}
                >
                  <Pagination
                    current={filters.event_page}
                    pageSize={50}
                    total={data?.event_total || 0}
                    showSizeChanger={false}
                    size="small"
                    onChange={(value) =>
                      setFilters({ ...filters, event_page: value })
                    }
                  />
                </nav>
              </>
            ),
          },
        ]}
      />
    </div>
  );
}
