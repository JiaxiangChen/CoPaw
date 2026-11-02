/**
 * 智能财富工作台 —— 任务页（今日任务 / 待触达客户 / 已完成）
 * 对应原型 tasksHTML：经营/客户双视角、任务树、重点标签表头筛选、
 * 经营方案/触达记录两类弹窗，执行列外链跳转电访与客户洞察。
 */
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { Tooltip } from "antd";
import { ChevronDown, RotateCw } from "lucide-react";
import cx from "classnames";
import DOMPurify from "dompurify";
import styles from "../index.module.less";
import { Icon } from "../components/Icon";
import { CustomerSchemePreview } from "../components/CustomerSchemePreview";
import { CustomerMetrics } from "../components/CustomerMetrics";
import { CustomerScenes } from "../components/CustomerScenes";
import { buildCustomerGroups } from "./customerGroups";
import { ContactStatusFilter } from "./ContactStatusFilter";
import { SceneFilter } from "./SceneFilter";
import { contactStatusOf, type ContactStatusFilterValue } from "./contactStatus";
import { buildInsightUrl, buildTelUrl, fetchSchemeSignature } from "../api";
import { useCanAccess, useWealthStore } from "../store";
import type { Customer } from "../types";
import { buildTaskTree, dateKey, scheduleLabel } from "../utils";
import { htmlPreviewEventsApi } from "@/api/modules/htmlPreviewEvents";
import type { HtmlTrackerPayloadType } from "@/api/types/htmlPreviewEvents";

/** 重点标签匹配：同一客户可能命中多个场景（标签以 "、" 分隔） */
function matchLabel(label: string, selected: string) {
  return label.split("、").includes(selected);
}

/** 标签筛选选项：从当前名单数据派生（客户视角为命中场景名） */
function labelOptionsOf(customers: Customer[]): string[] {
  const set = new Set<string>();
  for (const c of customers) {
    for (const part of c.label.split("、")) {
      if (part) set.add(part);
    }
  }
  return [...set];
}

export type TaskPageKind = "today" | "pending" | "done";

/** 任务树大类图标（对应 Icon 组件的命名） */
const CATEGORY_ICONS: Record<string, string> = {
  保险: "shield",
  贷款: "bank",
  存款: "safe",
  理财: "money",
  基金: "pie",
  代发: "user",
};

/** 树节点来源标签的配色：复用重点标签的既有色板 */
function sourceTagClass(source: string) {
  return source === "分行关注"
    ? styles.purple
    : source === "行长关注"
      ? styles.orange
      : "";
}

function OpportunityHtml({ value }: { value: string }) {
  const html = DOMPurify.sanitize(value, {
    ALLOWED_ATTR: [],
    FORBID_TAGS: [
      "style",
      "form",
      "input",
      "button",
      "textarea",
      "select",
      "option",
      "iframe",
      "object",
      "embed",
    ],
  });

  if (/^\s*<li(?:\s|>)/i.test(html)) {
    return (
      <ul
        className={styles.opportunityList}
        dangerouslySetInnerHTML={{ __html: html }}
      />
    );
  }

  return (
    <div
      className={styles.opportunityHtml}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

function OpportunityContent({
  items,
  multiple,
}: {
  items: string[];
  multiple?: boolean;
}) {
  if (multiple && items.length > 1) {
    return (
      <ul className={styles.opportunityList}>
        {items.map((item, index) => (
          <li key={index}>
            <OpportunityHtml value={item} />
          </li>
        ))}
      </ul>
    );
  }
  return <OpportunityHtml value={items[0]} />;
}

/** 经营机会单元格：支持外部接口返回的简单 HTML 列表。 */
export function Opportunities({
  customer,
  multiple,
}: {
  customer: Customer;
  multiple?: boolean;
}) {
  const items = (customer.opportunities ?? [customer.reason]).filter(
    (item) => item.trim().length > 0,
  );
  if (!items.length) return <>--</>;
  return (
    <Tooltip
      placement="topLeft"
      trigger={["hover", "focus"]}
      title={
        <div className={styles.opportunityTooltipContent}>
          <OpportunityContent items={items} multiple={multiple} />
        </div>
      }
    >
      <div className={styles.opportunityPreview} tabIndex={0}>
        <OpportunityContent items={items} multiple={multiple} />
      </div>
    </Tooltip>
  );
}

/**
 * 电访 / 客户洞察外链：取 get-sign 签名后拼接外链并新窗口打开。
 * signature 需异步获取，为避免弹窗被拦截，先同步打开空白窗口占位，
 * 随后异步填充 location 指向最终外链。
 */
async function openOutboundLink(kind: "dial" | "insight", c: Customer) {
  // 埋点：电访 button_id=phone，客户洞察 button_id=insight
  reportTaskClick(c, kind === "dial" ? "phone" : "insight");
  const win = window.open("", "_blank");
  try {
    // bbkOrgId / custUid 均由 /wealth/name-list 接口直接返回
    const bbkOrgId = c.bbkOrgId ?? "";
    const signature = await fetchSchemeSignature(c.custUid, bbkOrgId);
    const url =
      kind === "dial"
        ? buildTelUrl(c.custUid, bbkOrgId, signature)
        : buildInsightUrl(c.custUid, bbkOrgId, signature);
    if (win) {
      win.location.href = url;
    } else {
      window.open(url, "_blank");
    }
  } catch (error) {
    win?.close();
    console.warn(`[Wealth] 打开${kind === "dial" ? "电访" : "客户洞察"}外链失败`, error);
  }
}

/** 客户清单的洞察 / 电访点击；操作者与机构信息由 recordClick 补齐。 */
function reportTaskClick(c: Customer, buttonId: "phone" | "insight") {
  const buttonName = buttonId === "phone" ? "电访" : "客户洞察";
  const payload: HtmlTrackerPayloadType = {
    button_id: buttonId,
    button_name: buttonName,
    button_text: buttonName,
    button_type: buttonId,
    customer_id: c.custUid || null,
    customer_name: c.name || null,
    customer_info: {
      customer_id: c.custUid || "",
      name: c.name || "",
    },
    clicked_at: new Date().toISOString(),
    event_type: "button_click",
    page_source: "WP_AGENT_WORKSPACE_TASK_LIST",
    platform_source: "WP",
  };
  htmlPreviewEventsApi.recordClick(payload).catch(() => {
    // 埋点失败不影响主流程，静默丢弃
  });
}

function contactMethodLabel(method: string) {
  return method === "强接触" ? "已接触" : method;
}

function labelTagClass(label: string) {
  return label === "总行重点"
    ? styles.red
    : label === "行长指派"
      ? styles.orange
      : "";
}

/** 重点标签单元格：空值使用低强调度占位标签，避免表格出现空洞。 */
export function CustomerLabel({ label }: { label: string }) {
  const labels = label
    .split("、")
    .map((value) => value.trim())
    .filter(Boolean);
  return (
    <span className={styles.customerLabels}>
      {labels.length ? (
        labels.map((value) => (
          <span key={value} className={cx(styles.tag, labelTagClass(value))}>
            {value}
          </span>
        ))
      ) : (
        <span className={cx(styles.tag, styles.emptyTag)}>暂无标签</span>
      )}
    </span>
  );
}

export default function Tasks({ page }: { page: TaskPageKind }) {
  const canViewTasks = useCanAccess("tasks");
  const customers = useWealthStore((s) => s.customers);
  const counts = useWealthStore((s) =>
    page === "done"
      ? s.doneCounts
      : page === "pending"
        ? s.pendingCounts
        : s.todayCounts,
  );
  const customersLoading = useWealthStore((s) => s.customersLoading);
  const pendingCustomers = useWealthStore((s) => s.pendingCustomers);
  const pendingLoading = useWealthStore((s) => s.pendingLoading);
  const doneCustomers = useWealthStore((s) => s.doneCustomers);
  const doneLoading = useWealthStore((s) => s.doneLoading);
  const taskListsRefreshing = useWealthStore((s) => s.taskListsRefreshing);
  const plans = useWealthStore((s) => s.plans);
  const plansLoaded = useWealthStore((s) => s.plansLoaded);
  const refreshTodayCustomers = useWealthStore((s) => s.refreshTodayCustomers);
  const loadTodayCustomers = useWealthStore((s) => s.loadTodayCustomers);
  const loadPendingCustomers = useWealthStore((s) => s.loadPendingCustomers);
  const loadDoneCustomers = useWealthStore((s) => s.loadDoneCustomers);
  const openDialog = useWealthStore((s) => s.openDialog);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [view, setView] = useState<"business" | "customer">("business");
  const [taskLabel, setTaskLabel] = useState("全部");
  const [contactStatus, setContactStatus] = useState<ContactStatusFilterValue>(
    "全部",
  );
  const [search, setSearch] = useState("");
  const [sceneFilter, setSceneFilter] = useState<string | null>(null);
  const [selectedSceneId, setSelectedSceneId] = useState(
    () => searchParams.get("skillId") ?? "",
  );
  const [tagFilterOpen, setTagFilterOpen] = useState(false);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(
    new Set(),
  );
  const [tagFilterPos, setTagFilterPos] = useState({ left: 10, top: 10 });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const lastLoadedPageRef = useRef<TaskPageKind | null>(null);
  const lastLoadedViewRef = useRef(view);
  const groupTablesId = useId();
  /**
   * 经营方案 iframe 自增序列：作为 _t 时间戳附加到 URL 强制刷新加载，
   * 同时用作 <CustomerSchemePreview> 的 key 强制重挂载，从而重置内部的
   * retryCountRef / loading 状态（对齐 RM CustPlanCard 的 iframeKeyRef 机制，
   * 解决同一方案关闭后再点开不触发 iframe load 的问题）。
   */
  const schemeOpenSeqRef = useRef(0);

  const isBiz = view === "business" && page === "today";
  const doneView = page === "done";
  const showScenes = !isBiz;

  /** 工作任务树：从「已发布且今日有排程」的规划场景实时推导 */
  const today = dateKey(new Date());
  const taskTree = useMemo(() => buildTaskTree(plans, today), [plans, today]);
  const selectedNode =
    taskTree
      .flatMap((group) => group.nodes)
      .find((node) => node.sceneId === selectedSceneId) ??
    taskTree[0]?.nodes[0];
  const selectedSkillId = selectedNode?.sceneId;
  const selectedTask = selectedNode?.sceneName ?? "";

  // 规划变更/移除后选中节点可能失效：回退到树的第一个节点
  useEffect(() => {
    if (!isBiz || !plansLoaded) return;
    if (selectedSceneId !== (selectedSkillId ?? "")) {
      setSelectedSceneId(selectedSkillId ?? "");
    }
  }, [isBiz, plansLoaded, selectedSceneId, selectedSkillId]);

  /** 当前页的名单数据源：今日=customers；待触达/已完成=各自接口名单 */
  const loading =
    !plansLoaded ||
    (!taskListsRefreshing &&
      (page === "done"
        ? doneLoading
        : page === "pending"
          ? pendingLoading
          : customersLoading));
  const pool =
    loading
      ? []
      : page === "done"
        ? doneCustomers
        : page === "pending"
          ? pendingCustomers
          : customers;

  /** 原型 customerList：ignoreLabel 用于标签浮层计数 */
  const buildList = (ignoreLabel: boolean, ignoreContactStatus = false) => {
    let list = page === "pending" ? pool.filter((c) => !c.done) : pool;
    if (isBiz)
      list = list.filter((c) => c.skillId === selectedSkillId);
    if (showScenes && sceneFilter !== null)
      list = list.filter((c) => c.skillNames?.includes(sceneFilter));
    if (search.trim())
      list = list.filter((c) =>
        (
          c.name +
          c.reason +
          (c.opportunities ?? []).join(" ") +
          c.label +
          c.task
        ).includes(search.trim()),
      );
    if (!ignoreLabel && taskLabel !== "全部")
      list = list.filter((c) => matchLabel(c.label, taskLabel));
    if (page === "today" && !ignoreContactStatus && contactStatus !== "全部")
      list = list.filter((c) => contactStatusOf(c.touched) === contactStatus);
    return list;
  };

  const list = useMemo(
    () => buildList(false),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      page,
      pool,
      isBiz,
      selectedSkillId,
      showScenes,
      sceneFilter,
      search,
      taskLabel,
      contactStatus,
    ],
  );
  const groups = useMemo(() => {
    const grouped = buildCustomerGroups(list, isBiz);
    if (page === "today") {
      for (const group of grouped) {
        group.customers.sort(
          (a, b) => Number(a.touched === 1) - Number(b.touched === 1),
        );
      }
    }
    return grouped;
  }, [list, isBiz, page]);
  const showContactStatus = page === "today";
  const contactStatusCustomers = buildList(false, true);
  const sceneOptions = [...new Set(pool.flatMap((c) => c.skillNames ?? []))]
    .filter(Boolean);
  const columnCount = (isBiz ? 6 : 8) + Number(showContactStatus);
  const tableGroups = groups.length
    ? groups
    : [{ key: "empty", fields: [], customers: [] }];

  // 进入今日任务或切回经营视角更新规划；场景切换仅查询所选技能。
  // 绘制前启动查询，避免新视角短暂显示旧名单和统计。
  useLayoutEffect(() => {
    if (!plansLoaded) {
      return;
    }
    const enteringToday =
      page === "today" && lastLoadedPageRef.current !== "today";
    const switchingView = lastLoadedViewRef.current !== view;
    lastLoadedPageRef.current = page;
    lastLoadedViewRef.current = view;
    if (page === "today") {
      if (enteringToday || (view === "business" && switchingView)) {
        if (view === "business" && selectedSkillId)
          void refreshTodayCustomers(view, selectedSkillId);
        else void refreshTodayCustomers(view);
      } else if (view === "business" && selectedSkillId) {
        void loadTodayCustomers(view, selectedSkillId);
      } else {
        void loadTodayCustomers(view);
      }
    }
    if (page === "pending") {
      void loadPendingCustomers();
    }
    if (page === "done") {
      void loadDoneCustomers();
    }
  }, [
    page,
    view,
    selectedSkillId,
    plansLoaded,
    refreshTodayCustomers,
    loadTodayCustomers,
    loadPendingCustomers,
    loadDoneCustomers,
  ]);

  // 标签筛选浮层：外部点击 / Escape / 滚动 / 缩放时关闭
  useEffect(() => {
    if (!tagFilterOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (
        !popoverRef.current?.contains(e.target as Node) &&
        !triggerRef.current?.contains(e.target as Node)
      )
        setTagFilterOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setTagFilterOpen(false);
        triggerRef.current?.focus();
      }
    };
    const onScrollOrResize = () => setTagFilterOpen(false);
    document.addEventListener("click", onDocClick);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onScrollOrResize);
    window.addEventListener("scroll", onScrollOrResize, true);
    return () => {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onScrollOrResize);
      window.removeEventListener("scroll", onScrollOrResize, true);
    };
  }, [tagFilterOpen]);

  // 角色门禁：无任务页权限的角色回到看板（权限矩阵见 permissions.ts）
  if (!canViewTasks) {
    return <Navigate to="/wealth/board" replace />;
  }

  const toggleTagFilter = () => {
    if (tagFilterOpen) {
      setTagFilterOpen(false);
      return;
    }
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) {
      const width = 230;
      const height = 234;
      const left = Math.max(
        10,
        Math.min(rect.left, window.innerWidth - width - 10),
      );
      const top =
        rect.bottom + 8 + height > window.innerHeight - 10
          ? Math.max(10, rect.top - height - 8)
          : rect.bottom + 8;
      setTagFilterPos({ left, top });
    }
    setTagFilterOpen(true);
  };

  const applyTagFilter = (label: string) => {
    setTaskLabel(label);
    setTagFilterOpen(false);
    triggerRef.current?.focus();
  };

  const onPopoverKeyDown = (e: React.KeyboardEvent) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const choices = Array.from(
      popoverRef.current?.querySelectorAll<HTMLButtonElement>(
        `.${styles.tagFilterChoice}`,
      ) ?? [],
    );
    const index = choices.indexOf(document.activeElement as HTMLButtonElement);
    const next =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? choices.length - 1
          : e.key === "ArrowDown"
            ? (index + 1) % choices.length
            : (index + choices.length - 1) % choices.length;
    e.preventDefault();
    choices[next]?.focus();
  };

  const getCustomer = (id: string) => pool.find((c) => c.id === id);

  /**
   * 客户经营方案弹窗：优先以 iframe 渲染客户名单返回的 filename 链接
   * （/wealth/name-list 的 filename 为 reportView 完整 URL）；无链接时回退空态。
   */
  const showScheme = (id: string) => {
    const c = getCustomer(id);
    if (!c) return;
    // 埋点：查看经营方案 button_id=plan
    // reportTaskClick(c, "plan");
    if (!c.link) {
      openDialog({
        title: "客户经营方案",
        body: <div className={styles.empty}>暂无经营方案</div>,
        buttons: [{ label: "关闭" }],
      });
      return;
    }
    // 自增序列：既附加为 _t 参数强制 iframe 刷新加载，
    // 又作为组件 key 强制重挂载，重置内部重试/加载状态
    schemeOpenSeqRef.current += 1;
    const seq = schemeOpenSeqRef.current;
    let previewUrl = c.link;
    try {
      const u = new URL(c.link);
      u.searchParams.set("_t", String(seq));
      previewUrl = u.toString();
    } catch {
      previewUrl = `${c.link}${c.link.includes("?") ? "&" : "?"}_t=${seq}`;
    }
    // 经营方案以 iframe 铺满整个弹窗：不渲染标题头部、正文无内边距、
    // iframe 无边框。plain 弹窗通过弹窗类专用宽度铺满可用区域。
    openDialog({
      body: (
        <CustomerSchemePreview
          key={seq}
          url={previewUrl}
          custName={c.name}
          title={c.task || "客户经营方案"}
        />
      ),
      plain: true,
      buttons: [{ label: "关闭" }],
    });
  };

  /** 触达记录弹窗（原型 showResult） */
  const showResult = (id: string) => {
    const c = getCustomer(id);
    if (!c) return;
    openDialog({
      title: "客户触达记录",
      body: (
        <>
          <div className={styles.detailGrid}>
            <div>
              <small>客户姓名</small>
              <strong>{c.name}</strong>
            </div>
            <div>
              <small>触达方式</small>
              {contactMethodLabel(c.channel)}
            </div>
            <div>
              <small>完成时间</small>
              {c.time}
            </div>
            <div>
              <small>经营场景</small>
              {c.task}
            </div>
          </div>
          <h3>沟通记录</h3>
          <p style={{ marginTop: 10, whiteSpace: "pre-wrap" }}>{c.note}</p>
        </>
      ),
      buttons: [{ label: "关闭" }],
    });
  };

  const title =
    page === "pending"
      ? "待触达客户清单"
      : doneView
        ? "已完成任务"
        : isBiz
          ? "客户经营清单"
          : "今日客户经营清单";

  const filterActive = taskLabel !== "全部";

  return (
    <div className={cx(styles.tasksLayout, isBiz && styles.business)}>
      {isBiz && (
        <aside className={`${styles.panel} ${styles.taskTree}`}>
          <div className={styles.taskTreeHeading}>
            <h2>工作任务</h2>
            <Tooltip
              trigger={["hover", "focus"]}
              title={customersLoading ? "正在刷新工作任务" : "刷新工作任务"}
            >
              <span>
                <button
                  type="button"
                  className={styles.taskRefresh}
                  aria-label="刷新工作任务"
                  disabled={!plansLoaded || customersLoading}
                  aria-busy={customersLoading}
                  onClick={() =>
                    void (selectedSkillId
                      ? refreshTodayCustomers("business", selectedSkillId)
                      : refreshTodayCustomers("business"))
                  }
                >
                  <RotateCw
                    size={16}
                    aria-hidden="true"
                    className={
                      customersLoading ? styles.taskRefreshBusy : undefined
                    }
                  />
                </button>
              </span>
            </Tooltip>
          </div>
          {taskTree.length ? (
            taskTree.map((g) => (
              <details className={styles.treeGroup} key={g.category} open>
                <summary>
                  <Icon name={CATEGORY_ICONS[g.category] ?? "layer"} />
                  {g.category}
                  <Icon name="up" className={styles.arrow} />
                </summary>
                {g.nodes.map((n) => (
                  <Tooltip
                    key={n.sceneId}
                    placement="right"
                    trigger={["hover", "focus"]}
                    title={
                      <div className={styles.taskTreeTooltip}>
                        <div>技能名称：{n.sceneName}</div>
                        <div>来源规划：{n.planName}</div>
                      </div>
                    }
                  >
                    <button
                      className={cx(
                        styles.treeItem,
                        selectedSkillId === n.sceneId && styles.active,
                      )}
                      onClick={() => setSelectedSceneId(n.sceneId)}
                    >
                      <span
                        className={cx(styles.tag, sourceTagClass(n.source))}
                      >
                        {n.source}
                      </span>
                      <span className={styles.treeSkillName}>
                        {n.sceneName}
                      </span>
                    </button>
                  </Tooltip>
                ))}
              </details>
            ))
          ) : (
            <p className={styles.pageNote}>
              今日暂无排程中的经营场景，可在「创建计划」中新建并发布规划。
            </p>
          )}
        </aside>
      )}

      <section className={`${styles.panel} ${styles.taskPanel}`}>
        <div className={styles.sectionHead}>
          <h2>{title}</h2>
          {page === "today" ? (
            <div className={styles.switch} aria-label="任务视角">
              <button
                disabled={customersLoading}
                className={view === "business" ? styles.active : ""}
                onClick={() => {
                  setView("business");
                  setTaskLabel("全部");
                }}
              >
                经营视角
              </button>
              <button
                disabled={customersLoading}
                className={view === "customer" ? styles.active : ""}
                onClick={() => {
                  setView("customer");
                  setTaskLabel("全部");
                }}
              >
                客户视角
              </button>
            </div>
          ) : (
            <button
              className={`${styles.btn} ${styles.soft} ${styles.sm}`}
              onClick={() => navigate("/wealth/today")}
            >
              返回今日任务
            </button>
          )}
        </div>

        <div className={styles.taskSummary}>
          <div className={styles.bubble}>
            <Icon name={doneView ? "check" : "list"} />
          </div>
          <div>
            <h3 className={styles.taskSummaryTitle}>
              {doneView
                ? "客户经营任务已完成"
                : page === "pending"
                  ? "待处理：客户触达"
                  : isBiz
                    ? selectedTask
                      ? "当前任务：" + selectedTask
                      : "今日暂无排程中的任务"
                    : "今日任务：客户经营"}
              {isBiz &&
                !loading &&
                !customersLoading &&
                selectedNode &&
                !pool.length && (
                  <span className={styles.taskExecutionTime}>
                    执行时间：{scheduleLabel(selectedNode.schedule)}
                  </span>
                )}
            </h3>
            <div className={styles.summaryMetrics}>
              <span>
                目标客户 <b>{loading ? "--" : (counts?.targetCusCount ?? "--")}</b>{" "}
                人
              </span>
              <span>
                今日已触达{" "}
                <b>{loading ? "--" : (counts?.todayTouchCount ?? "--")}</b> 人
              </span>
              <span>
                七日内已触达{" "}
                <b>{loading ? "--" : (counts?.sevenDayTouchCount ?? "--")}</b> 人
              </span>
              <span>
                待处理{" "}
                <b className={styles.red}>
                  {loading ? "--" : (counts?.unTouchedCount ?? "--")}
                </b>{" "}
                人
              </span>
            </div>
          </div>
        </div>

        {page !== "today" && (
          <div className={styles.tableToolbar}>
            <span className={styles.subtle}>
              {doneView ? "触达记录与经营结果" : "优先处理尚未触达的客户"}
            </span>
            <input
              className={styles.input}
              aria-label="搜索客户或经营机会"
              placeholder="搜索客户、标签或经营机会"
              defaultValue={search}
              onChange={(e) => {
                setSearch(e.target.value);
              }}
            />
          </div>
        )}

        <div className={styles.customerGroups}>
          {tableGroups.map((group, index) => {
            const groupKey = `${selectedSkillId ?? ""}|${group.key}`;
            const collapsed = collapsedGroups.has(groupKey);
            const tableId = `${groupTablesId}-${index}`;
            const groupTitle = group.fields
              .map((field) => `${field.label}：${field.value}`)
              .join(" / ");
            return (
              <div key={groupKey} className={styles.customerGroup}>
                {group.fields.length > 0 && (
                  <h3 className={styles.customerGroupHeading}>
                    <button
                      type="button"
                      className={styles.customerGroupToggle}
                      aria-expanded={!collapsed}
                      aria-controls={tableId}
                      aria-label={`${groupTitle}，${group.customers.length} 人`}
                      onClick={() =>
                        setCollapsedGroups((current) => {
                          const next = new Set(current);
                          if (next.has(groupKey)) {
                            next.delete(groupKey);
                          } else {
                            next.add(groupKey);
                          }
                          return next;
                        })
                      }
                    >
                      <ChevronDown
                        size={18}
                        aria-hidden="true"
                        className={cx(
                          styles.customerGroupChevron,
                          collapsed && styles.customerGroupChevronCollapsed,
                        )}
                      />
                      <span className={styles.customerGroupTitle}>
                        {groupTitle}
                      </span>
                      <span className={styles.customerGroupCount}>
                        {group.customers.length} 人
                      </span>
                    </button>
                  </h3>
                )}
                <div
                  id={tableId}
                  className={styles.tableWrap}
                  hidden={collapsed}
                >
                  <table
                    aria-label={group.fields.length > 0 ? groupTitle : undefined}
                    className={cx(
                      styles.customerTable,
                      doneView
                        ? styles.completedTable
                        : styles.opportunityTable,
                      styles.metricsTable,
                      showScenes && styles.sceneTable,
                      page === "today" && !isBiz && styles.todayCustomerTable,
                      showContactStatus && styles.todayTaskTable,
                    )}
                  >
                    <thead>
                      <tr>
                        <th>客户姓名</th>
                        {!isBiz && (
                          <th scope="col" className={styles.tagFilterHeading}>
                            <button
                              type="button"
                              ref={triggerRef}
                              className={cx(
                                styles.tagFilterTrigger,
                                filterActive && styles.filtered,
                              )}
                              aria-label={
                                "筛选重点标签" +
                                (filterActive ? `，当前：${taskLabel}` : "")
                              }
                              aria-haspopup="dialog"
                              aria-expanded={tagFilterOpen}
                              aria-controls="wealthTagFilterPopover"
                              onClick={toggleTagFilter}
                            >
                              <span>重点标签</span>
                              <span className={styles.tagFilterIcon}>
                                <svg viewBox="0 0 20 20" aria-hidden="true">
                                  <path d="M3 4h14l-5.5 6v5l-3 1v-6Z" />
                                </svg>
                                {filterActive && <i></i>}
                              </span>
                            </button>
                            {filterActive && (
                              <div className={styles.tagFilterApplied}>
                                <span>{taskLabel}</span>
                                <button
                                  type="button"
                                  aria-label="清除重点标签筛选"
                                  title="清除筛选"
                                  onClick={() => applyTagFilter("全部")}
                                >
                                  ×
                                </button>
                              </div>
                            )}
                          </th>
                        )}
                        {showScenes && (
                          <th scope="col" className={styles.sceneCol}>
                            <SceneFilter
                              value={sceneFilter}
                              options={sceneOptions}
                              onChange={setSceneFilter}
                            />
                          </th>
                        )}
                        {!doneView && (
                          <th className={styles.opportunityCol}>经营机会</th>
                        )}
                        <th scope="col" className={styles.metricsCol}>
                          客户指标
                        </th>
                        {doneView && (
                          <th className={styles.channelCol}>触达方式</th>
                        )}
                        {showContactStatus && (
                          <th
                            scope="col"
                            className={cx(
                              styles.contactStatusCol,
                              styles.tagFilterHeading,
                            )}
                          >
                            <ContactStatusFilter
                              value={contactStatus}
                              customers={contactStatusCustomers}
                              onChange={setContactStatus}
                            />
                          </th>
                        )}
                        <th scope="col" className={styles.recentContactCol}>
                          最近接触时间
                        </th>
                        {!doneView && (
                          <th className={styles.schemeCol}>经营方案</th>
                        )}
                        <th className={styles.executionCol}>
                          {doneView ? "完成时间" : "执行"}
                        </th>
                        {doneView && <th>经营结果</th>}
                      </tr>
                    </thead>
                    <tbody>
                      {group.customers.length ? (
                        group.customers.map((c) => (
                          <tr key={c.id}>
                            <td className={styles.name}>{c.name}</td>
                            {!isBiz && (
                              <td>
                                <CustomerLabel label={c.label} />
                              </td>
                            )}
                            {showScenes && (
                              <td className={styles.sceneCol}>
                                <CustomerScenes key={c.id} names={c.skillNames} />
                              </td>
                            )}
                            {!doneView && (
                              <td className={styles.reason}>
                                <Opportunities customer={c} multiple={!isBiz} />
                              </td>
                            )}
                            <td className={styles.metricsCol}>
                              <CustomerMetrics
                                key={c.id}
                                fields={c.dynamicFields}
                              />
                            </td>
                            {doneView && (
                              <td className={styles.channelCol}>
                                <Icon
                                  name={
                                    c.channel === "电话"
                                      ? "phone"
                                      : c.channel === "企微"
                                      ? "chat"
                                      : "user"
                                  }
                                />
                                {"　"}
                                {contactMethodLabel(c.channel)}
                              </td>
                            )}
                            {showContactStatus && (
                              <td className={styles.contactStatusCol}>
                                <span
                                  className={cx(
                                    styles.tag,
                                    c.touched === 1
                                      ? styles.green
                                      : styles.emptyTag,
                                  )}
                                >
                                  {contactStatusOf(c.touched)}
                                </span>
                              </td>
                            )}
                            <td
                              className={styles.recentContactCol}
                              title={c.time || undefined}
                            >
                              <span className={styles.recentContactValue}>
                                {c.time || "—"}
                              </span>
                            </td>
                            {!doneView && (
                              <td className={styles.schemeCol}>
                                <button
                                  className={styles.link}
                                  onClick={() => showScheme(c.id)}
                                >
                                  查看经营方案
                                </button>
                              </td>
                            )}
                            <td className={styles.executionCol}>
                              {doneView ? (
                                c.time
                              ) : (
                                <div className={styles.contactActions}>
                                  <button
                                    className={`${styles.btn} ${styles.primary}`}
                                    onClick={() => openOutboundLink("dial", c)}
                                  >
                                    <Icon name="phone" />
                                    电访
                                  </button>
                                  <button
                                    className={styles.btn}
                                    onClick={() =>
                                      openOutboundLink("insight", c)
                                    }
                                  >
                                    <Icon name="user" />
                                    客户洞察
                                  </button>
                                </div>
                              )}
                            </td>
                            {doneView && (
                              <td>
                                <button
                                  className={styles.link}
                                  onClick={() => showResult(c.id)}
                                >
                                  查看记录
                                </button>
                              </td>
                            )}
                          </tr>
                        ))
                      ) : (
                        <tr>
                          <td colSpan={columnCount}>
                            <div
                              className={styles.empty}
                              role={loading ? "status" : undefined}
                            >
                              {loading
                                ? "客户清单加载中…"
                                : isBiz && selectedTask && !search.trim() && contactStatus === "全部"
                                ? `「${selectedTask}」暂无客户名单，待定时任务执行后生成`
                                : "暂无符合条件的客户"}
                            </div>
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })}
        </div>

        <div className={styles.pagination}>
          <span className={styles.total}>
            共 {loading ? "--" : list.length} 条记录
          </span>
        </div>
      </section>

      {tagFilterOpen && !isBiz && (
        <div
          className={styles.tagFilterPopover}
          id="wealthTagFilterPopover"
          role="dialog"
          aria-label="筛选重点标签"
          ref={popoverRef}
          style={{ left: tagFilterPos.left, top: tagFilterPos.top }}
          onKeyDown={onPopoverKeyDown}
        >
          <div className={styles.tagFilterPopoverTitle}>
            筛选重点标签<span>单选</span>
          </div>
          <div>
            {["全部", ...labelOptionsOf(customers)].map((label) => {
              const base = buildList(true);
              const count =
                label === "全部"
                  ? base.length
                  : base.filter((c) => matchLabel(c.label, label)).length;
              return (
                <button
                  type="button"
                  key={label}
                  className={cx(
                    styles.tagFilterChoice,
                    taskLabel === label && styles.selected,
                  )}
                  aria-pressed={taskLabel === label}
                  onClick={() => applyTagFilter(label)}
                >
                  <span
                    className={
                      label === "全部"
                        ? styles.allTagText
                        : cx(styles.tag, labelTagClass(label))
                    }
                  >
                    {label === "全部" ? "全部标签" : label}
                  </span>
                  <span className={styles.tagFilterChoiceCount}>{count}</span>
                  <span
                    className={styles.tagFilterChoiceCheck}
                    aria-hidden="true"
                  >
                    {taskLabel === label ? "✓" : ""}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
