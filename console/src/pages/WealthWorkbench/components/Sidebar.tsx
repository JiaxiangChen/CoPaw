/**
 * 智能财富工作台 —— 侧边导航
 * 已按需求移除「业绩分析」入口与「今日完成率」圆环。
 * 任务导航组仅客户经理角色可见（原型 canAccessPage 门禁）。
 */
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import cx from "classnames";
import styles from "../index.module.less";
import { useCanAccess, useWealthStore } from "../store";
import { Icon } from "./Icon";

export function Sidebar({
  collapsed,
  onToggleCollapse,
}: {
  collapsed: boolean;
  onToggleCollapse: () => void;
}) {
  const todayCustomerCount = useWealthStore((s) => s.todayCustomerCount);
  const plansLoaded = useWealthStore((s) => s.plansLoaded);
  const pendingCustomers = useWealthStore((s) => s.pendingCustomers);
  const doneCustomers = useWealthStore((s) => s.doneCustomers);
  const loadPendingCustomers = useWealthStore((s) => s.loadPendingCustomers);
  const loadDoneCustomers = useWealthStore((s) => s.loadDoneCustomers);
  const loadTodayCustomerCount = useWealthStore(
    (s) => s.loadTodayCustomerCount,
  );
  const hasTasks = useCanAccess("tasks");
  const [tasksOpen, setTasksOpen] = useState(true);
  const location = useLocation();
  const navigate = useNavigate();

  const current = location.pathname;

  useEffect(() => {
    if (!hasTasks || !plansLoaded) return;
    void Promise.all([
      loadTodayCustomerCount(),
      loadPendingCustomers(),
      loadDoneCustomers(),
    ]);
  }, [
    hasTasks,
    plansLoaded,
    loadTodayCustomerCount,
    loadPendingCustomers,
    loadDoneCustomers,
  ]);

  const navClass = (path: string) =>
    cx(styles.nav, current === path && styles.active);

  return (
    <aside className={styles.sidebar}>
      <nav aria-label="主导航">
        <button
          className={navClass("/wealth/board")}
          title="规划看板"
          aria-current={current === "/wealth/board" ? "page" : "false"}
          onClick={() => navigate("/wealth/board")}
        >
          <Icon name="home" />
          <span className={styles.navLabel}>规划看板</span>
        </button>
        <button
          className={navClass("/wealth/create")}
          title="创建计划"
          aria-current={current === "/wealth/create" ? "page" : "false"}
          onClick={() => navigate("/wealth/create")}
        >
          <Icon name="plus" />
          <span className={styles.navLabel}>创建计划</span>
        </button>
        {hasTasks && (
          <>
            <div className={styles.navDivider}></div>
            <div className={styles.navGroup}>
              <button
                className={styles.nav}
                aria-expanded={tasksOpen}
                title="我的任务"
                onClick={() => setTasksOpen((v) => !v)}
              >
                <Icon name="list" />
                <span className={styles.navLabel}>我的任务</span>
                <Icon name="down" className={styles.chev} />
              </button>
              {tasksOpen && (
                <div>
                  <button
                    className={cx(
                      styles.nav,
                      styles.sub,
                      current === "/wealth/today" && styles.active,
                    )}
                    title="今日任务"
                    aria-current={
                      current === "/wealth/today" ? "page" : "false"
                    }
                    onClick={() => navigate("/wealth/today")}
                  >
                    <Icon name="list" />
                    <span className={styles.navLabel}>今日任务</span>
                    {todayCustomerCount > 0 && (
                      <span className={styles.badge}>{todayCustomerCount}</span>
                    )}
                  </button>
                  <button
                    className={cx(
                      styles.nav,
                      styles.sub,
                      current === "/wealth/pending" && styles.active,
                    )}
                    title="待触达客户"
                    aria-current={
                      current === "/wealth/pending" ? "page" : "false"
                    }
                    onClick={() => navigate("/wealth/pending")}
                  >
                    <Icon name="user" />
                    <span className={styles.navLabel}>待触达客户</span>
                    {pendingCustomers.length > 0 && (
                      <span className={styles.badge}>
                        {pendingCustomers.length}
                      </span>
                    )}
                  </button>
                  <button
                    className={cx(
                      styles.nav,
                      styles.sub,
                      current === "/wealth/done" && styles.active,
                    )}
                    title="已完成"
                    aria-current={current === "/wealth/done" ? "page" : "false"}
                    onClick={() => navigate("/wealth/done")}
                  >
                    <Icon name="check" />
                    <span className={styles.navLabel}>已完成</span>
                    {doneCustomers.length > 0 && (
                      <span className={styles.badge}>
                        {doneCustomers.length}
                      </span>
                    )}
                  </button>
                </div>
              )}
            </div>
          </>
        )}
      </nav>
      <div className={styles.sidebarBottom}>
        <button
          className={styles.collapse}
          onClick={onToggleCollapse}
          aria-label="收起或展开菜单"
        >
          <Icon name="menu" />
          <span>{collapsed ? "展开菜单" : "收起菜单"}</span>
        </button>
      </div>
    </aside>
  );
}
