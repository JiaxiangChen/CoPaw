import { useId, useState } from "react";
import { Tooltip } from "antd";
import styles from "../index.module.less";

const PREVIEW_COUNT = 2;

export function CustomerScenes({ names = [] }: { names?: string[] }) {
  const [expanded, setExpanded] = useState(false);
  const id = useId();
  const visible = expanded ? names : names.slice(0, PREVIEW_COUNT);
  if (!names.length) return <span className={styles.metricEmpty}>--</span>;

  return (
    <div>
      <ul id={id} className={styles.customerScenes}>
        {visible.map((name) => (
          <li key={name}>
            <Tooltip
              placement="topLeft"
              trigger={["hover", "focus"]}
              title={
                <div className={styles.opportunityTooltipContent}>{name}</div>
              }
            >
              <span className={styles.metricItem} tabIndex={0}>
                {name}
              </span>
            </Tooltip>
          </li>
        ))}
      </ul>
      {names.length > PREVIEW_COUNT && (
        <button
          type="button"
          className={styles.metricsToggle}
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? "收起" : `展开剩余 ${names.length - PREVIEW_COUNT} 项`}
        </button>
      )}
    </div>
  );
}
