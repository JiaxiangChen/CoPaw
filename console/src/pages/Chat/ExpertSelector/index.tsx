import { useState } from "react";
import { CheckOutlined, LoadingOutlined } from "@ant-design/icons";
import { Dropdown, Spin, Tooltip } from "antd";
import { SparkDownLine } from "@agentscope-ai/icons";
import { resolveExpertLabel, type SelectableExpert } from "../expertSelection";
import styles from "./index.module.less";

export interface ExpertSelectorProps {
  experts: SelectableExpert[];
  loading?: boolean;
  planModeEnabled: boolean;
  goalModeEnabled?: boolean;
  selectedExpertId: string | null;
  onChange: (expertId: string | null) => void;
  onDisablePlanMode: () => void | Promise<void>;
  disabled?: boolean;
  inline?: boolean;
  persistent?: boolean;
  boundExpertName?: string | null;
  unavailable?: boolean;
}

export default function ExpertSelector({
  experts,
  loading = false,
  planModeEnabled,
  goalModeEnabled = false,
  selectedExpertId,
  onChange,
  onDisablePlanMode,
  disabled = false,
  inline = false,
  persistent = false,
  boundExpertName,
  unavailable = false,
}: ExpertSelectorProps) {
  const [open, setOpen] = useState(false);
  const selectedExpert =
    experts.find((expert) => expert.id === selectedExpertId) || null;

  const selectionDisabled = disabled || goalModeEnabled;

  const handleSelect = async (expert: SelectableExpert) => {
    if (selectionDisabled) return;
    if (planModeEnabled) {
      try {
        await onDisablePlanMode();
      } catch {
        return;
      }
    }
    onChange(expert.id);
    setOpen(false);
  };

  const label = selectedExpert ? resolveExpertLabel(selectedExpert) : "专家";

  const menu = (
    <div
      className={inline ? styles.quickMenuPanel : styles.panel}
      role="menu"
      aria-label="专家选择"
    >
      {persistent && selectedExpertId && (!selectedExpert || unavailable) && (
        <button type="button" className={styles.item} role="menuitem" disabled>
          <span className={styles.itemName}>
            {boundExpertName || selectedExpert?.name || selectedExpertId}
            （不可用）
          </span>
        </button>
      )}
      {loading ? (
        <div className={styles.state}>
          <Spin size="small" />
        </div>
      ) : experts.length === 0 ? (
        <div className={styles.state}>暂无可用专家</div>
      ) : (
        experts.map((expert) => (
          <button
            key={expert.id}
            type="button"
            className={`${styles.item} ${
              expert.id === selectedExpertId ? styles.itemActive : ""
            }`}
            disabled={
              selectionDisabled ||
              (unavailable && expert.id === selectedExpertId)
            }
            onClick={() => handleSelect(expert)}
            role="menuitem"
          >
            <span className={styles.itemText}>
              <span className={styles.itemName}>{expert.name}</span>
              {expert.description ? (
                <span className={styles.itemDescription}>
                  {expert.description}
                </span>
              ) : null}
            </span>
            {expert.id === selectedExpertId ? (
              <CheckOutlined className={styles.check} aria-hidden />
            ) : null}
          </button>
        ))
      )}
    </div>
  );

  if (inline) {
    return menu;
  }

  return (
    <Dropdown
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
      }}
      trigger={["click"]}
      placement="bottomRight"
      disabled={disabled || goalModeEnabled}
      dropdownRender={() => menu}
    >
      <Tooltip title="选择专家" mouseEnterDelay={0.5}>
        <button
          type="button"
          className={`${styles.trigger} ${open ? styles.triggerActive : ""}`}
          disabled={disabled || goalModeEnabled}
          aria-label={selectedExpert ? `已选择专家 ${label}` : "选择专家"}
        >
          {loading ? <LoadingOutlined className={styles.loading} /> : null}
          <span className={styles.triggerName}>{label}</span>
          <SparkDownLine
            className={`${styles.arrow} ${open ? styles.arrowOpen : ""}`}
          />
        </button>
      </Tooltip>
    </Dropdown>
  );
}
