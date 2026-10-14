/**
 * 智能财富工作台 —— 分发目标选择器（支行行长 / 分行中台发布规划时使用）
 * 用户池来自 distributeTargets.ts（按登录机构查询岗位人员，查不到时显示无数据），
 * 选中结果写入 store.targetSapIds，发布规划时随请求上传。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Cascader } from "antd";
import cx from "classnames";
import styles from "../index.module.less";
import { useWealthStore } from "../store";
import {
  fetchDistributeBranches,
  type DistributeBranch,
} from "../distributeTargets";
import { Icon } from "./Icon";

interface BranchOption extends DistributeBranch {
  value: string;
  label: string;
  isLeaf: boolean;
  children?: BranchOption[];
}

const branchOptions = (items: DistributeBranch[]): BranchOption[] =>
  items.map((branch) => ({
    ...branch,
    value: branch.mOrgCode,
    label: `${branch.mOrgName}（${branch.mOrgCode}）`,
    // 121516 按业务规则直接视为末级，其余机构通过下级空数组确认。
    isLeaf: branch.mOrgCode === "121516",
  }));

function updateBranchPath(
  options: BranchOption[],
  path: string[],
  patch: Partial<BranchOption>,
): BranchOption[] {
  return options.map((option) => {
    if (option.value !== path[0]) return option;
    if (path.length === 1) return { ...option, ...patch };
    return {
      ...option,
      children: updateBranchPath(option.children ?? [], path.slice(1), patch),
    };
  });
}

export function TargetPicker() {
  const isMiddle = useWealthStore((s) => s.accountId === "middle");
  const targetBranch = useWealthStore((s) => s.targetBranch);
  const branchTargetSapIds = useWealthStore((s) => s.branchTargetSapIds);
  const targets = useWealthStore((s) => s.targets);
  const targetsLoaded = useWealthStore((s) => s.targetsLoaded);
  const targetSapIds = useWealthStore((s) => s.targetSapIds);
  const loadTargets = useWealthStore((s) => s.loadTargets);
  const resetTargetBranch = useWealthStore((s) => s.resetTargetBranch);
  const toggleTarget = useWealthStore((s) => s.toggleTarget);
  const [keyword, setKeyword] = useState("");
  const [branches, setBranches] = useState<BranchOption[]>([]);
  const [branchesLoaded, setBranchesLoaded] = useState(false);
  const [branchOpen, setBranchOpen] = useState(false);
  const [branchPath, setBranchPath] = useState<BranchOption[]>([]);
  const [branchError, setBranchError] = useState(false);
  const [branchReload, setBranchReload] = useState(0);
  const branchRequestSeq = useRef(0);

  useEffect(() => {
    if (!isMiddle) {
      void loadTargets();
      return;
    }
    let cancelled = false;
    branchRequestSeq.current += 1;
    resetTargetBranch();
    setBranchPath([]);
    setBranchError(false);
    setBranchesLoaded(false);
    void fetchDistributeBranches().then((items) => {
      if (cancelled) return;
      setBranchError(items === null);
      setBranches(branchOptions(items ?? []));
      setBranchesLoaded(true);
    });
    return () => {
      cancelled = true;
      branchRequestSeq.current += 1;
    };
  }, [isMiddle, loadTargets, resetTargetBranch, branchReload]);

  const selectBranchPath = async (values: string[], path: BranchOption[]) => {
    const branch = path[path.length - 1];
    if (!branch) return;
    const requestSeq = ++branchRequestSeq.current;
    setBranchPath(path);
    setBranchError(false);
    setKeyword("");
    resetTargetBranch();
    // 当前级人员与下级机构独立加载，可停留在任意一级选择人员。
    void loadTargets(branch);
    if (branch.isLeaf) {
      setBranchOpen(false);
      return;
    }
    if (branch.children?.length) {
      // 等待搜索结果选择完成后展开已缓存的下一层。
      await Promise.resolve();
      if (requestSeq === branchRequestSeq.current) setBranchOpen(true);
      return;
    }
    const children = await fetchDistributeBranches(branch);
    if (requestSeq !== branchRequestSeq.current) return;
    if (children === null) {
      setBranchOpen(false);
      setBranchError(true);
      return;
    }
    setBranchOpen(children.length > 0);
    setBranches((items) =>
      updateBranchPath(items, values, {
        children: children.length ? branchOptions(children) : undefined,
        isLeaf: children.length === 0,
      }),
    );
  };

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
      <div className={styles.targetToolbar}>
        {isMiddle && (
          <div className={styles.field}>
            <label htmlFor="distribute-branch">选择支行</label>
            <Cascader<BranchOption>
              id="distribute-branch"
              className={styles.branchSelect}
              dropdownStyle={{
                maxWidth: "calc(100vw - 32px)",
                overflowX: "auto",
              }}
              aria-label="选择支行"
              open={branchOpen}
              onDropdownVisibleChange={setBranchOpen}
              changeOnSelect
              allowClear={false}
              value={branchPath.map((branch) => branch.value)}
              disabled={!branchesLoaded || branches.length === 0}
              placeholder={
                !branchesLoaded
                  ? "正在加载支行…"
                  : branches.length
                  ? "搜索或逐级选择支行"
                  : branchError
                  ? "支行加载失败"
                  : "当前分行暂无可选支行"
              }
              notFoundContent="未找到匹配的支行"
              options={branches}
              showSearch={{
                filter: (input, path) =>
                  path.some((option) =>
                    option.label
                      .toLowerCase()
                      .includes(input.trim().toLowerCase()),
                  ),
              }}
              onChange={(values, path) => {
                void selectBranchPath(values as string[], path);
              }}
            />
          </div>
        )}
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

      {branchError && (
        <div role="alert" className={styles.field}>
          <span>
            {branchPath.length
              ? "下级支行查询失败，请重试"
              : "支行查询失败，请重试"}
          </span>
          <button
            type="button"
            className={styles.btn}
            onClick={() => {
              if (branchPath.length)
                void selectBranchPath(
                  branchPath.map((branch) => branch.value),
                  branchPath,
                );
              else setBranchReload((value) => value + 1);
            }}
          >
            重试
          </button>
        </div>
      )}

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
