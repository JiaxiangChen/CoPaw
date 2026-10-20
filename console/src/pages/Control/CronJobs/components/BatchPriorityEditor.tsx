import { useEffect, useId, useState } from "react";
import { Alert, Button, Select } from "antd";
import { ArrowUp, ArrowDown, X } from "lucide-react";
import { BBK_ID_MAP } from "../../../../constants/bbk";
import {
  batchOperations,
  type BatchPriority,
} from "../../../../api/modules/batchOperations";
import styles from "./BatchPriorityEditor.module.less";

function readBatchPriority(value: unknown): BatchPriority {
  const raw =
    value && typeof value === "object" ? (value as Partial<BatchPriority>) : {};
  return {
    user_ids: Array.isArray(raw.user_ids)
      ? raw.user_ids.filter((x): x is string => typeof x === "string")
      : [],
    branch_ids: Array.isArray(raw.branch_ids)
      ? raw.branch_ids.filter((x): x is string => typeof x === "string")
      : [],
  };
}

export default function BatchPriorityEditor({
  jobId,
  initial,
  disabled = false,
  onSaved,
  onDirtyChange,
  savePriority,
}: {
  jobId: string;
  initial: unknown;
  disabled?: boolean;
  onSaved?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
  /** 新页注入独立管理接口；未传入时保留原页面协议。 */
  savePriority?: (priority: BatchPriority) => Promise<unknown>;
}) {
  const fieldId = useId();
  const [priority, setPriority] = useState(() => readBatchPriority(initial));
  const [savedPriority, setSavedPriority] = useState(() =>
    JSON.stringify(readBatchPriority(initial)),
  );
  const dirty = JSON.stringify(priority) !== savedPriority;
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    onDirtyChange?.(dirty || saving);
    return () => onDirtyChange?.(false);
  }, [dirty, saving, onDirtyChange]);
  const [notice, setNotice] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);
  const busy = disabled || saving;
  const change = (field: keyof BatchPriority, values: string[]) => {
    setNotice(null);
    setPriority((old) => ({ ...old, [field]: Array.from(new Set(values)) }));
  };
  const move = (field: keyof BatchPriority, index: number, offset: number) => {
    const values = [...priority[field]];
    [values[index], values[index + offset]] = [
      values[index + offset],
      values[index],
    ];
    change(field, values);
  };
  const save = async () => {
    setSaving(true);
    setNotice(null);
    try {
      if (savePriority) await savePriority(priority);
      else await batchOperations.savePriority(jobId, priority);
      setSavedPriority(JSON.stringify(priority));
      setNotice({ type: "success", text: "优先策略已保存，从新批次生效" });
      onSaved?.();
    } catch (error) {
      setNotice({
        type: "error",
        text: error instanceof Error ? error.message : "保存失败",
      });
    } finally {
      setSaving(false);
    }
  };
  return (
    <section className={styles.editor} aria-label="批次内优先策略">
      <header className={styles.heading}>
        <h3 className={styles.title}>批次内优先策略</h3>
        <p className={styles.hint}>
          用户优先级 &gt; 一级分行优先级 &gt; 默认排序
        </p>
        <p className={styles.hint}>
          仅调整已有接收人的执行顺序，不增加接收人。
        </p>
      </header>
      <div className={styles.fields}>
        {(["user_ids", "branch_ids"] as const).map((field) => {
          const label = field === "user_ids" ? "优先用户" : "优先一级分行";
          return (
            <div key={field} className={styles.field}>
              <div className={styles.fieldHeading}>
                <label htmlFor={`${fieldId}-${field}`}>{label}</label>
                <span className={styles.hint}>从上到下优先</span>
              </div>
              <Select
                id={`${fieldId}-${field}`}
                aria-label={label}
                className={styles.select}
                mode={field === "user_ids" ? "tags" : "multiple"}
                placeholder={
                  field === "user_ids"
                    ? "输入用户 ID，按回车添加"
                    : "选择一级分行"
                }
                options={field === "branch_ids" ? BBK_ID_MAP : undefined}
                maxTagCount={0}
                maxTagPlaceholder={() => `已选 ${priority[field].length} 项`}
                value={priority[field]}
                onChange={(values) => change(field, values)}
                disabled={busy}
              />
              {priority[field].length ? (
                <ol
                  className={styles.list}
                  aria-label={`${label}排序`}
                  tabIndex={0}
                >
                  {priority[field].map((id, index) => (
                    <li key={id} className={styles.row}>
                      <span className={styles.rank} aria-hidden="true">
                        {index + 1}
                      </span>
                      <span className={styles.name}>
                        {field === "branch_ids"
                          ? BBK_ID_MAP.find((b) => b.value === id)?.label || id
                          : id}
                      </span>
                      <div className={styles.rowActions}>
                        <Button
                          type="text"
                          size="small"
                          aria-label={`${label} ${id} 上移`}
                          icon={<ArrowUp size={14} />}
                          disabled={busy || index === 0}
                          onClick={() => move(field, index, -1)}
                        />
                        <Button
                          type="text"
                          size="small"
                          aria-label={`${label} ${id} 下移`}
                          icon={<ArrowDown size={14} />}
                          disabled={
                            busy || index === priority[field].length - 1
                          }
                          onClick={() => move(field, index, 1)}
                        />
                        <Button
                          type="text"
                          size="small"
                          aria-label={`${label} ${id} 移除`}
                          icon={<X size={14} />}
                          disabled={busy}
                          onClick={() =>
                            change(
                              field,
                              priority[field].filter((v) => v !== id),
                            )
                          }
                        />
                      </div>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className={styles.empty}>暂未设置{label}</p>
              )}
            </div>
          );
        })}
      </div>
      {notice && <Alert type={notice.type} message={notice.text} showIcon />}
      <footer className={styles.footer}>
        <div className={styles.hint} aria-live="polite">
          <div>保存后对新批次生效</div>
          {dirty && <div className={styles.dirty}>有未保存的更改</div>}
        </div>
        <Button type="primary" onClick={save} loading={saving} disabled={busy}>
          保存优先策略
        </Button>
      </footer>
    </section>
  );
}
