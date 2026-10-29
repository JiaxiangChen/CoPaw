import { useCallback, useState } from "react";
import { Alert, Button, Empty, Skeleton, Space, Tag } from "antd";
import { cronTaskTracking } from "../../../api/modules/cronTaskTracking";
import type {
  TaskIdentity,
  TrackingFilters,
  TrackingNode,
} from "../../../api/types/cronTaskTracking";
import { Coverage } from "./TrackingOverview";
import { useTrackingResource } from "./useTrackingResource";
import { color, entries, statuses, text, time } from "./format";
import styles from "./index.module.less";

function TimelineNode({ node }: { node: TrackingNode }) {
  const outcome = node.recorded_outcome;
  return (
    <li className={styles.node}>
      <div className={styles.nodeTime}>
        <time dateTime={node.occurred_at}>{time(node.occurred_at)}</time>
        <span>北京时间</span>
      </div>
      <article className={styles.nodeBody}>
        <div className={styles.nodeTitle}>
          <h3>{node.title}</h3>
          {outcome ? (
            <Tag color={color(outcome)}>{statuses[outcome] || outcome}</Tag>
          ) : null}
          <Tag>{entries[node.entry] || "入口未确认"}</Tag>
        </div>
        {node.time_basis === "scheduler_confirmed" ? (
          <p className={styles.muted}>确认时间，非异步实际完成时间</p>
        ) : null}
        {node.current_status && node.category === "management" ? (
          <p>
            当前记录结果：{statuses[node.current_status] || node.current_status}
            （不是提交时的处理结果）
          </p>
        ) : null}
        <p className={styles.wrap}>
          原因：{text(node.reason)}
          {node.reason_basis === "derived_business_type"
            ? "（按操作类型说明）"
            : ""}
        </p>
        {node.execution_id ? (
          <p className={styles.mono}>执行 ID：{node.execution_id}</p>
        ) : null}
        {node.gaps.length ? (
          <p className={styles.muted}>缺失：{node.gaps.join("、")}</p>
        ) : null}
        <details>
          <summary>查看详情</summary>
          <dl className={styles.detailGrid}>
            <dt>操作人 / 上下文</dt>
            <dd>
              {text(node.actor_id)} ·{" "}
              {node.actor_kind === "operator"
                ? "操作发起人"
                : node.actor_kind === "system"
                ? "系统同步"
                : "不作为实际操作人证明"}
            </dd>
            <dt>操作 / 回执 / 历史 ID</dt>
            <dd>
              {[node.operation_id, node.receipt_id, node.history_id]
                .map(text)
                .join(" / ")}
            </dd>
            <dt>证据与来源</dt>
            <dd>
              {text(node.evidence)} ·{" "}
              {node.source_refs
                .map((ref) => `${ref.table} (${ref.key})`)
                .join(" · ")}
            </dd>
          </dl>
          <pre>{JSON.stringify(node.details, null, 2)}</pre>
        </details>
      </article>
    </li>
  );
}

export default function TrackingTimeline({
  source,
  identity,
  filters,
}: {
  source: string;
  identity: TaskIdentity;
  filters: TrackingFilters;
}) {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [page, setPage] = useState(0);
  const [retry, setRetry] = useState(0);
  const cursor = cursors[page];
  const load = useCallback(
    (signal: AbortSignal) =>
      cronTaskTracking.timeline(
        source,
        identity,
        { ...filters, limit: 50, cursor },
        signal,
      ),
    [source, identity, filters, cursor],
  );
  const resource = useTrackingResource(load, retry);
  const value = resource.value;
  const next = () => {
    if (!value?.next_cursor) return;
    setCursors((old) => [...old.slice(0, page + 1), value.next_cursor!]);
    setPage(page + 1);
  };
  return (
    <section aria-label="任务时间轴" aria-busy={resource.loading}>
      <div className={styles.sectionTitle}>
        <h2>时间轴</h2>
        <span className={styles.muted}>
          {value ? `本页 ${value.loaded_count} 条 · 第 ${page + 1} 页` : ""}
        </span>
      </div>
      <Coverage value={value} />
      {resource.loading ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : resource.error ? (
        <Alert
          showIcon
          type="error"
          message="时间轴加载失败"
          description={resource.error}
          action={<Button onClick={() => setRetry(retry + 1)}>重试查询</Button>}
        />
      ) : value?.items.length ? (
        <ol className={styles.timeline}>
          {value.items.map((node) => (
            <TimelineNode key={node.event_id} node={node} />
          ))}
        </ol>
      ) : (
        <Empty description="所选范围内暂无记录，不代表任务从未执行" />
      )}
      <div className={styles.pagination}>
        <span className={styles.muted}>
          只读查询，不修改未读状态；新记录通过刷新加载。
        </span>
        <Space>
          <Button
            disabled={page === 0 || resource.loading}
            onClick={() => setPage(page - 1)}
          >
            上一页
          </Button>
          <Button
            disabled={!value?.has_more || resource.loading}
            onClick={next}
          >
            下一页
          </Button>
        </Space>
      </div>
    </section>
  );
}
