import { Tooltip } from "antd";
import { getBbkDisplayName } from "../../../constants/bbk";
import type { ManagedBatchMember } from "../../../api/modules/managedBatchBoard";
import styles from "../CronBatchDispatch/index.module.less";

/** 沿用旧版中文优先详情，顺位仍取新版受理时冻结值。 */
export default function PriorityOrder({ item }: { item: ManagedBatchMember }) {
  const p = item.dispatch_priority;
  const branch = p?.branch_id ? String(p.branch_id) : "";
  const label =
    p?.basis === "user"
      ? `用户优先 · 第${p.user_rank}位`
      : p?.basis === "branch"
      ? `分行优先 · ${getBbkDisplayName(branch)} · 第${p.branch_rank}位`
      : "默认排序";
  return (
    <Tooltip
      trigger={["hover", "focus"]}
      placement="topLeft"
      title={
        <div>
          <div>优先依据：{label}</div>
          <div>用户顺位：{String(p?.user_rank ?? "未设置")}</div>
          <div>分行顺位：{String(p?.branch_rank ?? "未设置")}</div>
          <div>
            分行：
            {branch ? `${getBbkDisplayName(branch)}（${branch}）` : "未知"}
          </div>
          <div>热度：{item.viewer_heat_score ?? "-"}</div>
        </div>
      }
    >
      <span
        tabIndex={0}
        className={styles.intentOrder}
        aria-label={`批内顺位 ${item.member_order + 1}，查看优先详情`}
      >
        {item.member_order + 1}
      </span>
    </Tooltip>
  );
}
