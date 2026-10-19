import { ChevronDown } from "lucide-react";
import type { CronDispatchCapacityItem } from "../../../api/modules/monitor";
import styles from "../CronBatchDispatch/index.module.less";
import { timeText } from "./format";

/** 旧版结构化Worker详情布局；不改变容量统计或调节行为。 */
export default function WorkerAdjustmentDetails({
  item,
}: {
  item: CronDispatchCapacityItem;
}) {
  const rows = [
    ["记录 ID", item.id],
    ["Worker ID", item.worker_id || "-"],
    ["Source", item.source_id || "-"],
    ["策略", item.strategy_id || "-"],
    [
      "最小 / 基线 / 最大",
      `${item.min_workers} / ${item.baseline_workers} / ${item.max_workers}`,
    ],
    ["调整前 / 调整后", `${item.previous_workers} / ${item.effective_workers}`],
    [
      "等待 / 已领取 / 运行",
      `${item.pending_count} / ${item.claimed_count} / ${item.running_count}`,
    ],
    ["成功 / 失败", `${item.success_count} / ${item.failure_count}`],
    ["失败率", `${(item.error_rate * 100).toFixed(2)}%`],
    ["平均耗时", `${item.avg_latency_ms} ms`],
    ["记录时间", timeText(item.created_at)],
  ];
  return (
    <details className={styles.workerEvent}>
      <summary
        className={styles.workerEventSummary}
        aria-label={`查看调整记录 ${item.id}`}
      >
        <strong>{item.decision_reason || "-"}</strong>
        <span>
          {item.provider_id}/{item.model_id} · {item.previous_workers} →{" "}
          {item.effective_workers}
        </span>
        <em>{timeText(item.created_at)}</em>
        <ChevronDown
          size={15}
          className={styles.workerEventChevron}
          aria-hidden="true"
        />
      </summary>
      <dl className={styles.workerEventDetails}>
        {rows.map(([label, value]) => (
          <div
            key={label}
            className={
              label === "Worker ID" ? styles.workerEventIdentity : undefined
            }
          >
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
        <div className={styles.workerEventRule}>
          <dt>命中规则</dt>
          <dd>
            <pre>
              {item.matched_rule == null
                ? "-"
                : JSON.stringify(item.matched_rule, null, 2)}
            </pre>
          </dd>
        </div>
      </dl>
    </details>
  );
}
