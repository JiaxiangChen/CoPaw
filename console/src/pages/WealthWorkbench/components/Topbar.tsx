/**
 * 智能财富工作台 —— 顶部栏：品牌、标题、当前账户
 * 消息通知（铃铛入口）为原型示例、无真实接口，已注释隐藏；
 * 待通知接口确定后取消注释恢复，并对齐真实数据。
 */
// import { useNavigate } from "react-router-dom"; // 随通知入口一并注释
import styles from "../index.module.less";
import { getBbkDisplayName } from "@/constants/bbk";
import { useIframeStore } from "@/stores/iframeStore";
import { selectCurrentAccount, useWealthStore } from "../store";
import { canUseOperations } from "../operations";
import { OperationsControl } from "./OperationsControl";
import operationStyles from "./OperationsControl.module.less";
// import { useCanAccess } from "../store"; // 随通知入口一并注释
// import { Icon } from "./Icon"; // 随通知入口一并注释

/** 消息通知弹窗（原型 showNotifications）—— 原型示例，暂无真实通知接口，暂时隐藏
function NotificationsDialogBody() {
  const hasTasks = useCanAccess("tasks");
  return (
    <>
      <div className={styles.recommendation}>
        <h3>本月经营规划已更新</h3>
        <p>查看规划看板，了解当前工作重点。</p>
      </div>
      {hasTasks ? (
        <div className={styles.recommendation}>
          <h3>今日客户清单已准备</h3>
          <p>目标客户名单已生成，请前往「今日任务」查看并跟进。</p>
        </div>
      ) : (
        <div className={styles.recommendation}>
          <h3>工作重点支持日历查看</h3>
          <p>可按周、月查看工作任务安排及执行频率。</p>
        </div>
      )}
      <p className={styles.pageNote}>通知为原型示例。</p>
    </>
  );
}
*/

export function Topbar() {
  const account = useWealthStore(selectCurrentAccount);
  const bbkId = useIframeStore((state) => state.bbk);
  const branchName = bbkId ? getBbkDisplayName(bbkId) : "";
  // 以下通知相关状态随入口一并注释，恢复时取消注释
  // const openDialog = useWealthStore((s) => s.openDialog);
  // const closeDialog = useWealthStore((s) => s.closeDialog);
  // const hasTasks = useCanAccess("tasks");
  // const navigate = useNavigate();

  // const showNotifications = () => {
  //   openDialog({
  //     title: "消息通知",
  //     body: <NotificationsDialogBody />,
  //     buttons: [
  //       { label: "关闭" },
  //       {
  //         label: hasTasks ? "查看今日任务" : "查看规划看板",
  //         primary: true,
  //         onClick: () => {
  //           closeDialog();
  //           navigate(hasTasks ? "/wealth/today" : "/wealth/board");
  //         },
  //       },
  //     ],
  //   });
  // };

  return (
    <header className={styles.topbar}>
      <div className={styles.brand}>
        <svg
          className={styles.brandmark}
          viewBox="0 0 42 42"
          aria-hidden="true"
        >
          <circle
            cx="21"
            cy="21"
            r="18"
            fill="none"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            d="M31 12C18 2 5 25 17 29c7 2 12-6 14-10L14 24c-2-7 3-13 8-11"
            fill="none"
            stroke="currentColor"
            strokeWidth="4"
            strokeLinecap="round"
          />
        </svg>
        <div>
          <strong>智拓</strong>
        </div>
      </div>
      <div
        className={`${styles.brandtitle} ${
          canUseOperations() ? operationStyles.compactTitle : ""
        }`}
      >
        智能财富管理工作台
      </div>
      <div className={styles.account}>
        <OperationsControl />
        {/* 消息通知入口：原型示例、无真实接口，暂时隐藏；待接口确定后恢复
        <button
          className={`${styles.iconbtn} ${styles.bell}`}
          aria-label="查看通知"
          onClick={showNotifications}
        >
          <Icon name="bell" />
        </button>
        <span className={styles.sep}></span>
        */}
        {/* <span className={`${styles.department} ${styles.subtle}`}>
          {[account?.name, branchName].filter(Boolean).join(" · ")}
        </span> */}
      </div>
    </header>
  );
}
