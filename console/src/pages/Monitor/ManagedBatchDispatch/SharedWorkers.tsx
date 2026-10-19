import { useState } from "react";
import { Alert, Empty, Pagination, Progress, Spin, Tag, Tooltip } from "antd";
import { ChevronDown } from "lucide-react";
import type {
  CronDispatchWorkersResponse,
  CronDispatchCapacityItem,
} from "../../../api/modules/monitor";
import WorkerHistoryChart from "../CronBatchDispatch/WorkerHistoryChart";
import styles from "../CronBatchDispatch/index.module.less";
import WorkerAdjustmentDetails from "./WorkerAdjustmentDetails";

function CapacityRow({
  item,
  history,
}: {
  item: CronDispatchCapacityItem;
  history: CronDispatchCapacityItem[];
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div>
      <button
        type="button"
        className={styles.capacityRow}
        aria-label={`查看 ${item.provider_id} / ${item.model_id} 的 Worker 调整过程`}
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className={styles.capacityMeta}>
          <strong>{item.model_id}</strong>
          <span>{item.provider_id}</span>
        </span>
        <Progress
          percent={Math.min(
            100,
            Math.round(
              (item.effective_workers / Math.max(item.max_workers, 1)) * 100,
            ),
          )}
          showInfo={false}
          size="small"
        />
        <span className={styles.capacityValue}>
          <strong>{item.effective_workers}</strong>
          <span>{item.decision_reason || "未记录原因"}</span>
        </span>
        <ChevronDown
          size={15}
          className={styles.capacityChevron}
          aria-hidden="true"
        />
      </button>
      {expanded && (
        <WorkerHistoryChart
          items={history.filter(
            (row) =>
              row.provider_id === item.provider_id &&
              row.model_id === item.model_id,
          )}
        />
      )}
    </div>
  );
}

export default function SharedWorkers({
  value,
  loading,
  error,
}: {
  value?: CronDispatchWorkersResponse | null;
  loading: boolean;
  error?: string;
}) {
  const [page, setPage] = useState(1);
  const events = value?.capacity_events || [];
  const current = Math.min(page, Math.max(1, Math.ceil(events.length / 5)));
  return (
    <section className={styles.strategyGrid}>
      <article className={styles.panel}>
        <div className={styles.panelHeader}>
          <div>
            <h2>Source 维度模型策略</h2>
            <span>共享模型池：新旧调度共同使用策略和容量</span>
          </div>
        </div>
        <Spin spinning={loading}>
          <div className={styles.policyGridList}>
            {value?.policies.map((policy) => (
              <article
                className={styles.policyCard}
                key={JSON.stringify([policy.provider_id, policy.model_id])}
              >
                <div className={styles.policyHead}>
                  <div>
                    <h3>{policy.provider_id}</h3>
                    <p>{policy.model_id}</p>
                  </div>
                  <Tag>{policy.enabled ? "启用" : "停用"}</Tag>
                </div>
                <div className={styles.policyGrid}>
                  <span>默认策略</span>
                  <strong>{policy.default_strategy_id}</strong>
                  <span>最小 / 基线 / 最大</span>
                  <strong>
                    {String(policy.strategy?.min_workers ?? "-")} /{" "}
                    {String(policy.strategy?.baseline_workers ?? "-")} /{" "}
                    {String(policy.strategy?.max_workers ?? "-")}
                  </strong>
                  <span>调整间隔</span>
                  <strong>
                    {String(policy.strategy?.adjust_interval_seconds ?? "-")}s
                  </strong>
                  <span>反馈窗口</span>
                  <strong>
                    {String(policy.strategy?.feedback_window_seconds ?? "-")}s
                  </strong>
                </div>
                <Tooltip
                  title={
                    <pre>
                      {JSON.stringify(policy.strategy_schedule, null, 2)}
                    </pre>
                  }
                >
                  <div className={styles.policyJson}>
                    schedule={JSON.stringify(policy.strategy_schedule)}
                  </div>
                </Tooltip>
                <Tooltip
                  title={
                    <pre>
                      {JSON.stringify(
                        policy.strategy?.error_rate_rules,
                        null,
                        2,
                      )}
                    </pre>
                  }
                >
                  <div className={styles.policyJson}>
                    rules={JSON.stringify(policy.strategy?.error_rate_rules)}
                  </div>
                </Tooltip>
              </article>
            ))}
          </div>
          {!loading && !value?.policies.length && (
            <Empty description="暂无模型策略" />
          )}
        </Spin>
      </article>
      <article className={styles.panel}>
        <div className={styles.panelHeader}>
          <div>
            <h2>Worker 变动</h2>
            <span>共享容量；完整游标历史加载成功后才展示曲线</span>
          </div>
        </div>
        {error ? (
          <Alert type="error" message={`容量历史不完整：${error}`} />
        ) : (
          <Spin spinning={loading}>
            <div className={styles.capacityList}>
              {value?.current_capacity.map((item) => (
                <CapacityRow
                  key={JSON.stringify([item.provider_id, item.model_id])}
                  item={item}
                  history={events}
                />
              ))}
            </div>
            {!loading && (
              <>
                {!value?.current_capacity.length && (
                  <Empty description="暂无 capacity" />
                )}
                <div className={styles.subSectionTitle}>
                  区间内调整记录（{events.length} 条）
                </div>
                <div className={styles.workerEventList}>
                  {events.slice((current - 1) * 5, current * 5).map((event) => (
                    <WorkerAdjustmentDetails key={event.id} item={event} />
                  ))}
                </div>
                <Pagination
                  aria-label="新版容量记录分页"
                  current={current}
                  pageSize={5}
                  total={events.length}
                  showSizeChanger={false}
                  onChange={setPage}
                />
              </>
            )}
          </Spin>
        )}
      </article>
    </section>
  );
}
