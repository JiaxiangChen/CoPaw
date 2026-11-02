import { Select, Switch } from "antd";
import { useNavigate } from "react-router-dom";
import {
  canUseOperations,
  getOperationsBranches,
  useWealthOperations,
} from "../operations";
import { useWealthStore } from "../store";
import styles from "./OperationsControl.module.less";

export function OperationsControl() {
  const navigate = useNavigate();
  const enabled = useWealthOperations((state) => state.enabled);
  const branchId = useWealthOperations((state) => state.branchId);
  const acting = useWealthStore((state) => state.acting);
  if (!canUseOperations()) return null;

  const changeMode = (nextEnabled: boolean, nextBranch: string | null) => {
    const state = useWealthStore.getState();
    if (state.acting) return;
    const apply = () => {
      void state.setOperationsMode(nextEnabled, nextBranch);
      navigate("/wealth/board");
    };
    if (
      state.draft.name.trim() ||
      state.draft.items.length ||
      state.editingId ||
      state.targetSapIds.length
    ) {
      state.openDialog({
        title: "切换工作环境",
        body: "切换将清空当前草稿和分发选择，是否继续？",
        buttons: [
          { label: "取消" },
          { label: "清空并切换", danger: true, onClick: apply },
        ],
      });
    } else apply();
  };

  return (
    <div className={styles.controls}>
      <label className={styles.toggle}>
        <span id="wealth-operations-label">运维模式</span>
        <Switch
          size="small"
          aria-labelledby="wealth-operations-label"
          checked={enabled}
          disabled={acting}
          onChange={(checked) => changeMode(checked, null)}
        />
      </label>
      {enabled && (
        <>
          <Select
            className={styles.branch}
            aria-label="运维分行"
            placeholder="请选择分行"
            value={branchId ?? undefined}
            options={getOperationsBranches()}
            showSearch
            optionFilterProp="label"
            disabled={acting}
            onChange={(value) => changeMode(true, value)}
            notFoundContent="暂无可选分行"
          />
          <span className={styles.role}>分行中台</span>
        </>
      )}
    </div>
  );
}
