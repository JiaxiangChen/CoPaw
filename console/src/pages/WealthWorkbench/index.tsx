/**
 * 智能财富工作台 —— 独立路由入口与页面骨架
 *
 * 挂在 App.tsx 的 /wealth/* 下，不经过 MainLayout，视觉与交互以原型为准。
 * 嵌套路由：board / create / today / pending / done（/wealth 默认进 board）。
 * 角色预览切换时通过 key 重挂载页面子树，还原原型"切换角色重置页面视图状态"的行为。
 */
import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useSearchParams } from "react-router-dom";
import cx from "classnames";
import styles from "./index.module.less";
import { useWealthStore, useCanAccess } from "./store";
import { DialogHost } from "./components/DialogHost";
import { Icon, IconDefs } from "./components/Icon";
import { Sidebar } from "./components/Sidebar";
import { Ticker } from "./components/Ticker";
import { Topbar } from "./components/Topbar";
import Board from "./Board";
import Create from "./Create";
import Tasks from "./Tasks";
import { useVisibilityRefresh } from "./useVisibilityRefresh";
import { useIframeStore } from "@/stores/iframeStore";
import {
  canUseOperations,
  operationsIdentity,
  useWealthOperations,
} from "./operations";
import operationStyles from "./components/OperationsControl.module.less";

export default function WealthWorkbench() {
  useVisibilityRefresh();
  const [searchParams, setSearchParams] = useSearchParams();
  const messageHideLogo = useIframeStore((s) => s.hideLogo);
  const [entryHideLogo] = useState(
    () => searchParams.get("hideLogo") === "true",
  );
  const hideHeader = searchParams.has("hideLogo")
    ? searchParams.get("hideLogo") === "true"
    : entryHideLogo || messageHideLogo;

  // 内部导航可能省略查询参数；补回隐藏设置，使嵌入布局保持一致。
  useEffect(() => {
    if (entryHideLogo && hideHeader && !searchParams.has("hideLogo")) {
      setSearchParams(
        (params) => {
          params.set("hideLogo", "true");
          return params;
        },
        { replace: true },
      );
    }
  }, [entryHideLogo, hideHeader, searchParams, setSearchParams]);
  const initialized = useWealthStore((s) => s.initialized);
  const accountId = useWealthStore((s) => s.accountId);
  const contextVersion = useWealthStore((s) => s.contextVersion);
  const operationsEnabled = useWealthOperations((s) => s.enabled);
  const operationsBranch = useWealthOperations((s) => s.branchId);
  const identity = useIframeStore(operationsIdentity);
  const setOperationsMode = useWealthStore((s) => s.setOperationsMode);
  const init = useWealthStore((s) => s.init);
  // 任务三页仅客户经理可访问（权限矩阵见 permissions.ts），直接输 URL 也重定向回看板
  const allowTasks = useCanAccess("tasks");
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    void init();
  }, [init]);

  useEffect(() => {
    const operations = useWealthOperations.getState();
    if (
      operations.enabled &&
      (hideHeader || !canUseOperations() || operations.identity !== identity)
    ) {
      void setOperationsMode(false, null, true).then(() => init());
    }
  }, [hideHeader, identity, init, operationsEnabled, setOperationsMode]);

  useEffect(
    () => () => {
      if (useWealthOperations.getState().enabled) {
        void useWealthStore.getState().setOperationsMode(false, null, true);
      }
    },
    [],
  );

  if (!initialized) {
    return <div className={styles.root} />;
  }

  // 未识别身份（positionId 缺失或未命中映射）：整页拦截，不渲染任何业务数据
  if (accountId === "unknown") {
    return (
      <div className={styles.root}>
        <IconDefs />
        {!hideHeader && canUseOperations() && <Topbar />}
        <div className={styles.denied}>
          <Icon name="shield" className={styles.deniedIcon} />
          <div className={styles.deniedTitle}>暂无访问权限</div>
          <div className={styles.deniedDesc}>
            当前登录岗位未纳入智能财富工作台使用范围，请联系管理员开通。
          </div>
        </div>
      </div>
    );
  }

  const taskRoute = (page: "today" | "pending" | "done") =>
    allowTasks ? (
      <Tasks page={page} />
    ) : (
      <Navigate to="/wealth/board" replace />
    );

  return (
    <div
      className={cx(
        styles.root,
        collapsed && styles.collapsed,
        hideHeader && styles.withoutHeader,
      )}
    >
      <IconDefs />
      {!hideHeader && <Topbar />}
      {!hideHeader && <Ticker />}
      {!hideHeader && (
        <Sidebar
          collapsed={collapsed}
          onToggleCollapse={() => setCollapsed((v) => !v)}
        />
      )}
      <main className={styles.main} id="main">
        <div
          className={styles.page}
          key={`${accountId}-${contextVersion ?? 0}`}
        >
          {operationsEnabled && !operationsBranch ? (
            <div className={operationStyles.empty} role="status">
              <strong>运维模式已开启</strong>
              请在右上角选择分行，随后可按分行中台流程创建和分发规划。
            </div>
          ) : (
            <Routes>
              <Route index element={<Navigate to="board" replace />} />
              <Route path="board" element={<Board />} />
              <Route path="create" element={<Create />} />
              <Route path="today" element={taskRoute("today")} />
              <Route path="pending" element={taskRoute("pending")} />
              <Route path="done" element={taskRoute("done")} />
              <Route path="*" element={<Navigate to="board" replace />} />
            </Routes>
          )}
        </div>
      </main>
      <DialogHost />
    </div>
  );
}
