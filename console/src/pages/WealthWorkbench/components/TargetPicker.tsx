/**
 * 智能财富工作台 —— 分发目标选择器（支行行长 / 分行中台发布规划时使用）
 * 用户池来自 distributeTargets.ts（按登录机构查询岗位人员，查不到时显示无数据），
 * 选中结果写入 store.targetSapIds，发布规划时随请求上传。
 */
import { useEffect, useMemo, useState } from "react";
import cx from "classnames";
import styles from "../index.module.less";
import { useWealthStore } from "../store";
import {
  fetchDistributeBranches,
  type DistributeBranch,
} from "../distributeTargets";
import { Icon } from "./Icon";

export function TargetPicker() {
  const isMiddle = useWealthStore((s) => s.accountId === "middle");
  const targetBranch = useWealthStore((s) => s.targetBranch);
  const branchTargetSapIds = useWealthStore((s) => s.branchTargetSapIds);
  const targets = useWealthStore((s) => s.targets);
  const targetsLoaded = useWealthStore((s) => s.targetsLoaded);
  const targetSapIds = useWealthStore((s) => s.targetSapIds);
  const loadTargets = useWealthStore((s) => s.loadTargets);
  const toggleTarget = useWealthStore((s) => s.toggleTarget);
  const [keyword, setKeyword] = useState("");
  const [branches, setBranches] = useState<DistributeBranch[]>([]);
  const [branchesLoaded, setBranchesLoaded] = useState(false);

  useEffect(() => {
    if (!isMiddle) {
      void loadTargets();
      return;
    }
    let cancelled = false;
    setBranchesLoaded(false);
    void fetchDistributeBranches().then((items) => {
      if (cancelled) return;
      setBranches(items);
      setBranchesLoaded(true);
    });
    return () => {
      cancelled = true;
    };
  }, [isMiddle, loadTargets]);

  const visible = useMemo(() => {
    const candidates = isMiddle
      ? targets.filter((t) => branchTargetSapIds.includes(t.sapId))
      : targets;
    const kw = keyword.trim().toLowerCase();
    if (!kw) return candidates;
    return candidates.filter(
      (t) =>
        t.name.toLowerCase().includes(kw) ||
        t.sapId.toLowerCase().includes(kw) ||
        t.orgName.toLowerCase().includes(kw),
    );
  }, [targets, keyword, isMiddle, branchTargetSapIds]);

  const selected = targets.filter((t) => targetSapIds.includes(t.sapId));
  const allVisibleSelected =
    visible.length > 0 && visible.every((t) => targetSapIds.includes(t.sapId));

  return (
    <div className={styles.targetPicker}>
      {isMiddle && (
        <div className={styles.field}>
          <label htmlFor="distribute-branch">选择支行</label>
          <select
            id="distribute-branch"
            className={styles.input}
            value={targetBranch?.mOrgCode ?? ""}
            disabled={!branchesLoaded || branches.length === 0}
            onChange={(e) => {
              const branch = branches.find(
                (item) => item.mOrgCode === e.target.value,
              );
              if (!branch) return;
              setKeyword("");
              void loadTargets(branch);
            }}
          >
            <option value="" disabled>
              {!branchesLoaded
                ? "正在加载支行…"
                : branches.length
                ? "请选择支行"
                : "当前分行暂无可选支行"}
            </option>
            {branches.map((branch) => (
              <option key={branch.mOrgCode} value={branch.mOrgCode}>
                {branch.mOrgName}（{branch.mOrgCode}）
              </option>
            ))}
          </select>
        </div>
      )}
      <div className={styles.targetToolbar}>
        <input
          className={styles.input}
          placeholder="搜索姓名 / 用户ID / 机构"
          aria-label="搜索分发目标"
          value={keyword}
          disabled={isMiddle && !targetBranch}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <button
          type="button"
          className={styles.btn}
          disabled={!targetsLoaded || visible.length === 0}
          onClick={() => {
            for (const target of visible) {
              if (allVisibleSelected || !targetSapIds.includes(target.sapId)) {
                toggleTarget(target.sapId);
              }
            }
          }}
        >
          {allVisibleSelected ? "取消全选" : "全选"}
          {keyword.trim() ? "搜索结果" : ""}
        </button>
        <span className={styles.targetCount}>
          已选 <strong>{targetSapIds.length}</strong> 人
        </span>
      </div>

      <div className={styles.targetList} role="group" aria-label="分发目标列表">
        {isMiddle && !targetBranch ? (
          <div className={styles.empty}>请先选择支行，再选择分发人员</div>
        ) : !targetsLoaded ? (
          <div className={styles.empty}>正在加载用户列表…</div>
        ) : visible.length ? (
          visible.map((t) => {
            const chosen = targetSapIds.includes(t.sapId);
            return (
              <button
                key={t.sapId}
                className={cx(styles.targetItem, chosen && styles.active)}
                aria-pressed={chosen}
                onClick={() => toggleTarget(t.sapId)}
              >
                <span className={styles.targetCheck}>{chosen ? "✓" : ""}</span>
                <span className={styles.targetAvatar}>
                  {t.name.slice(0, 1)}
                </span>
                <span className={styles.targetInfo}>
                  <strong>{t.name}</strong>
                  <small>
                    {t.sapId}
                    {t.orgName ? ` · ${t.orgName}` : ""}
                  </small>
                </span>
              </button>
            );
          })
        ) : (
          <div className={styles.empty}>
            {keyword ? "未找到匹配的用户" : "本支行暂无可分发的用户"}
          </div>
        )}
      </div>

      {selected.length > 0 && (
        <div className={styles.targetChips}>
          {selected.map((t) => (
            <span key={t.sapId} className={styles.targetChip}>
              {t.name}
              <button
                aria-label={`移除${t.name}`}
                onClick={() => toggleTarget(t.sapId)}
              >
                <Icon name="close" />
              </button>
            </span>
          ))}
        </div>
      )}
      <p className={styles.pageNote}>
        {isMiddle && "可切换支行继续选择，已选人员会累计保留。"}
        发布后规划将下发给所选客户经理；客户经理角色的规划默认发给自己，无需此步骤。
      </p>
    </div>
  );
}
