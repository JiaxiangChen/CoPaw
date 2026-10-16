/**
 * 智能财富工作台 —— 规划看板页
 * 对应原型 boardHTML：周期筛选、本期规划概览、当前工作重点（日历/列表双模式）。
 */
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import cx from "classnames";
import { DatePicker, Tooltip } from "antd";
import { RotateCw } from "lucide-react";
import dayjs from "dayjs";
import styles from "../index.module.less";
import { Icon } from "../components/Icon";
import {
  addCalendarDays,
  calendarDate,
  calendarRange,
  cycleRange,
  dateKey,
  planFrequency,
  planPublishLabel,
  planScheduledOn,
  scheduleLabel,
  todayKey,
} from "../utils";
import { selectCurrentAccount, useWealthStore } from "../store";
import { fetchAvailableSceneCount } from "../api";
import type { Plan } from "../types";
import { BOARD_SOURCES_BY_ROLE } from "../permissions";
import { Calendar } from "./Calendar";
import { usePlanStats } from "./usePlanStats";
import { useBoardRefresh } from "./useBoardRefresh";

const PERIODS = ["本季", "本月", "本周", "今日", "T+1日", "自定义"];

/** 状态着色：失败→红；发布中/分发中→蓝；已下发等稳定态→默认 */
function statusClass(status: string) {
  if (status.includes("失败")) return styles.red;
  if (status.includes("发布中") || status.includes("分发中")) {
    return styles.blue;
  }
  return "";
}

/** 列表模式 / 详情弹窗共用的规划操作按钮语义由父级注入 */

export default function Board() {
  const account = useWealthStore(selectCurrentAccount);
  const boardSources = BOARD_SOURCES_BY_ROLE[account.id];
  const boardTabs = ["全部", ...boardSources];
  const plans = useWealthStore((s) => s.plans);
  const plansLoaded = useWealthStore((s) => s.plansLoaded);
  const openDialog = useWealthStore((s) => s.openDialog);
  const closeDialog = useWealthStore((s) => s.closeDialog);
  const editPlan = useWealthStore((s) => s.editPlan);
  const removePlan = useWealthStore((s) => s.removePlan);
  const refreshPlans = useWealthStore((s) => s.refreshPlans);
  const navigate = useNavigate();

  const [boardTab, setBoardTab] = useState("全部");
  const [period, setPeriod] = useState("本月");
  const [customRange, setCustomRange] = useState<[string, string]>(
    () => cycleRange("本月")!,
  );
  const [boardMode, setBoardMode] = useState<"calendar" | "list">("calendar");
  const [dimension, setDimension] = useState<"week" | "month">("week");
  const [anchor, setAnchor] = useState(todayKey);
  const [availableScenes, setAvailableScenes] = useState<number | null>(null);

  // 「可用能力」统计：全部大类的场景技能总数；接口不可达时保持 "--" 占位
  useEffect(() => {
    let alive = true;
    void fetchAvailableSceneCount().then((count) => {
      if (alive) setAvailableScenes(count);
    });
    return () => {
      alive = false;
    };
  }, []);

  const range: [string, string] =
    boardMode === "calendar"
      ? calendarRange(anchor, dimension)
      : period === "自定义"
      ? customRange
      : cycleRange(period) ?? [todayKey(), todayKey()];
  const [rangeStart, rangeEnd] = range;

  const all = useMemo(
    () =>
      plans.filter(
        (p) =>
          boardSources.includes(p.source) &&
          p.start <= rangeEnd &&
          p.end >= rangeStart,
      ),
    [plans, boardSources, rangeStart, rangeEnd],
  );
  const visible = useMemo(
    () => all.filter((p) => boardTab === "全部" || p.source === boardTab),
    [all, boardTab],
  );

  // 覆盖经营场景数（本地去重统计）；任务执行率依赖任务实例接口，保持占位
  const sceneCount = new Set(
    all.flatMap((p) => p.items?.map((x) => x.id) ?? []),
  ).size;

  const hasActive = plans.some(
    (p) =>
      p.publishStatus === "publishing" ||
      p.status.includes("发布中") ||
      p.status.includes("分发中"),
  );
  const { refreshing, statsRefreshVersion, manualRefresh } = useBoardRefresh(
    hasActive,
    refreshPlans,
  );

  // 目标客户 / 已生成任务：按规划 ID 批量查询统计接口；
  // 接口不可达时 planStat 返回 null，对应位置保持 "--" 占位
  const { sceneStats, statsLoading, statsUpdating, statsFailed } = usePlanStats(
    all,
    plans,
    account.id,
    statsRefreshVersion,
  );

  const boardRefreshing = refreshing || statsLoading || statsUpdating;

  /** 直接使用接口返回的规划统计，不再按场景累加。 */
  const planStat = (p: Plan): { customers: number; tasks: number } | null => {
    const hit = sceneStats?.[p.id];
    return hit
      ? { customers: hit.targetCustomerCount, tasks: hit.generatedTaskCount }
      : null;
  };

  const customersText = (p: Plan) => {
    const stat = planStat(p);
    return stat == null ? "--" : `${stat.customers} 人`;
  };
  const tasksText = (p: Plan) => {
    const stat = planStat(p);
    return stat == null ? "--" : `${stat.tasks} 个`;
  };
  const generatedTotal =
    sceneStats == null || all.some((p) => planStat(p) == null)
      ? null
      : all.reduce((sum, p) => sum + (planStat(p)?.tasks ?? 0), 0);

  const onEditPlan = (id: string) => {
    editPlan(id);
    navigate("/wealth/create");
  };

  const onRemovePlan = (id: string) => {
    const p = plans.find((x) => x.id === id);
    if (!p) return;
    openDialog({
      title: "移除规划",
      body: (
        <>
          <p>确定移除「{p.name}」？</p>
          <p>移除后，该规划将不再显示在当前工作重点中。</p>
        </>
      ),
      buttons: [
        { label: "取消" },
        {
          label: "确认移除",
          danger: true,
          onClick: () => void removePlan(id),
        },
      ],
    });
  };

  /** 工作任务详情弹窗（原型 showCalendarDetails） */
  const showCalendarDetails = (day: string, id?: string) => {
    const entries = all.filter(
      (p) =>
        (boardTab === "全部" || p.source === boardTab) &&
        planScheduledOn(p, day) &&
        (id === undefined || p.id === id),
    );
    openDialog({
      title: "工作任务详情",
      wide: true,
      body: (
        <>
          <div className={styles.calendarDetailCaption}>
            <span>
              {day.replace(/-/g, " / ")}
              {"　·　"}
              {id === undefined
                ? "当日工作任务"
                : entries[0]?.name ?? "工作任务"}
            </span>
            <span>共 {entries.length} 项</span>
          </div>
          <div className={styles.tableWrap}>
            <table className={styles.calendarDetailTable}>
              <thead>
                <tr>
                  <th>工作任务</th>
                  <th>关注来源</th>
                  <th>经营方向</th>
                  <th>执行时间</th>
                  <th>执行频率</th>
                  <th>目标客户</th>
                  <th>已生成任务</th>
                  {/* <th>任务执行率</th> */}
                  <th>发布状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {entries.length ? (
                  entries.map((p) => (
                    <tr key={p.id}>
                      <td className={styles.detailName}>{p.name}</td>
                      <td>
                        <span
                          className={cx(
                            styles.tag,
                            p.source === "分行关注"
                              ? styles.red
                              : styles.purple,
                          )}
                        >
                          {p.source}
                        </span>
                      </td>
                      <td className={styles.detailDirection}>
                        {p.desc}
                        {p.items?.length ? (
                          <div className={styles.detailTaskSchedules}>
                            {p.items.map((x, i) => (
                              <span key={x.id}>
                                {i > 0 && <br />}
                                {x.sceneName}：{x.start} 至 {x.end} ·{" "}
                                {scheduleLabel(x.schedule)}
                              </span>
                            ))}
                          </div>
                        ) : null}
                      </td>
                      <td className={styles.detailTime}>
                        {p.start}
                        <br />至 {p.end}
                      </td>
                      <td>
                        <span className={styles.tag}>{planFrequency(p)}</span>
                      </td>
                      <td>{customersText(p)}</td>
                      <td>{tasksText(p)}</td>
                      {/* <td>
                        <div className={styles.progressLine}>
                          <div className={styles.progress}>
                            <span style={{ width: `${p.rate}%` }}></span>
                          </div>
                          <b>{p.rate}%</b>
                        </div>
                      </td> */}
                      <td>
                        <span
                          className={cx(
                            styles.status,
                            statusClass(planPublishLabel(p)),
                          )}
                        >
                          <i className={styles.dot}></i>
                          {planPublishLabel(p)}
                        </span>
                      </td>
                      <td>
                        {p.editable !== false ? (
                          <div className={styles.actions}>
                            <button
                              className={`${styles.btn} ${styles.sm} ${styles.soft}`}
                              onClick={() => {
                                closeDialog();
                                onEditPlan(p.id);
                              }}
                            >
                              修改规划
                            </button>
                            <button
                              className={`${styles.btn} ${styles.sm}`}
                              onClick={() => onRemovePlan(p.id)}
                            >
                              移除规划
                            </button>
                          </div>
                        ) : (
                          <span className={styles.readonlyHint}>只读</span>
                        )}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={10}>
                      <div className={styles.empty}>
                        当日暂无符合筛选条件的工作任务
                      </div>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <p className={styles.pageNote}>
            详情与列表模式使用同一份规划信息，数据为整个规划执行周期的累计值。
          </p>
        </>
      ),
      buttons: [{ label: "关闭" }],
    });
  };

  const stats = [
    {
      icon: "list",
      n: plansLoaded ? String(all.length) : "--",
      title: "当前规划",
      detail: "本期经营方向",
      val: plansLoaded ? `${all.length} 项` : "加载中",
      up: false,
    },
    {
      icon: "layer",
      n: plansLoaded ? String(sceneCount) : "--",
      title: "覆盖经营场景",
      detail: "可用能力",
      val: availableScenes == null ? "--" : `${availableScenes} 个`,
      up: false,
    },
    {
      icon: "check",
      n:
        plansLoaded && generatedTotal != null
          ? generatedTotal.toLocaleString()
          : "--",
      title: "已生成任务",
      detail: "定时任务",
      val: "",
      // detail: "较上月",
      // val: "--",
      up: true,
    },
    // {
    //   icon: "users",
    //   // n: rate + "%",
    //   n: "--",
    //   title: "任务执行率",
    //   detail: "较上月",
    //   val: "--",
    //   up: true,
    // },
  ];

  return (
    <>
      <div className={styles.filters}>
        {boardMode === "list" ? (
          <>
            <span className={styles.label}>规划周期</span>
            <div className={styles.pills}>
              {PERIODS.map((p) => (
                <button
                  key={p}
                  className={cx(styles.pill, period === p && styles.active)}
                  aria-pressed={period === p}
                  onClick={() => setPeriod(p)}
                >
                  {p}
                </button>
              ))}
            </div>
          </>
        ) : (
          <span className={styles.label}>看板周期</span>
        )}
        <div
          className={cx(
            styles.datebox,
            boardMode === "list" && period === "自定义" && styles.customDatebox,
          )}
        >
          {boardMode === "list" && period === "自定义" ? (
            <DatePicker.RangePicker
              className={cx(styles.taskDatePicker, styles.boardDateRange)}
              classNames={{ popup: { root: styles.taskDatePopup } }}
              aria-label="自定义规划周期"
              inputReadOnly
              allowClear={false}
              format="YYYY-MM-DD"
              placeholder={["开始日期", "结束日期"]}
              value={[dayjs(customRange[0]), dayjs(customRange[1])]}
              onChange={(values) => {
                if (values?.[0] && values[1]) {
                  setCustomRange([
                    values[0].format("YYYY-MM-DD"),
                    values[1].format("YYYY-MM-DD"),
                  ]);
                }
              }}
            />
          ) : (
            <>
              <Icon name="calendar" />
              {range.map((x) => x.replace(/-/g, ".")).join(" — ")}
            </>
          )}
        </div>
        {/* 暂不需要展示岗位 */}
        {/* <div className={styles.role}>
          当前角色：{account?.role} <Icon name="building" />
        </div> */}
      </div>

      <section className={`${styles.panel} ${styles.overview}`}>
        <div className={styles.sectionHead}>
          <h2>本期规划概览</h2>
          <Tooltip
            placement="topRight"
            trigger={["hover", "focus"]}
            title={boardRefreshing ? "正在刷新看板" : "刷新看板"}
          >
            <span>
              <button
                type="button"
                className={styles.taskRefresh}
                aria-label="刷新看板"
                disabled={!plansLoaded || boardRefreshing}
                aria-busy={boardRefreshing}
                onClick={() => void manualRefresh()}
              >
                <RotateCw
                  size={16}
                  aria-hidden="true"
                  className={
                    boardRefreshing ? styles.taskRefreshBusy : undefined
                  }
                />
              </button>
            </span>
          </Tooltip>
        </div>
        {statsFailed && (
          <p className={styles.readonlyHint} role="status">
            {sceneStats === null
              ? "客户与任务统计加载失败"
              : "统计更新失败，暂显示上次数据"}
          </p>
        )}
        <div className={styles.stats}>
          {stats.map((s) => (
            <div className={styles.stat} key={s.title}>
              <div className={styles.bubble}>
                <Icon name={s.icon} />
              </div>
              <div>
                <strong>{s.n}</strong>
                <b>{s.title}</b>
                <p>
                  {s.detail}
                  <em className={s.up ? styles.up : ""}>{s.val}</em>
                </p>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className={`${styles.panel} ${styles.focus}`}>
        <div className={styles.sectionHead}>
          <h2>当前工作重点</h2>
          <div className={styles.tabs}>
            {boardTabs.map((t) => (
              <button
                key={t}
                className={cx(styles.tab, boardTab === t && styles.active)}
                onClick={() => setBoardTab(t)}
              >
                {t}（
                {plansLoaded
                  ? t === "全部"
                    ? all.length
                    : all.filter((p) => p.source === t).length
                  : "--"}
                ）
              </button>
            ))}
          </div>
          <div
            className={`${styles.switch} ${styles.boardViewSwitch}`}
            aria-label="工作重点展示模式"
          >
            <button
              className={boardMode === "calendar" ? styles.active : ""}
              aria-pressed={boardMode === "calendar"}
              onClick={() => setBoardMode("calendar")}
            >
              <Icon name="calendar" />
              看板模式
            </button>
            <button
              className={boardMode === "list" ? styles.active : ""}
              aria-pressed={boardMode === "list"}
              onClick={() => setBoardMode("list")}
            >
              <Icon name="list" />
              列表模式
            </button>
          </div>
        </div>
        {!plansLoaded ? (
          <div className={styles.boardLoading} role="status">
            正在加载规划数据…
          </div>
        ) : boardMode === "calendar" ? (
          <Calendar
            sources={boardSources}
            visible={visible}
            planStat={planStat}
            dimension={dimension}
            anchor={anchor}
            onShift={(delta) => {
              if (dimension === "week") {
                setAnchor((a) => addCalendarDays(a, delta * 7));
              } else {
                setAnchor((a) => {
                  const d = calendarDate(a);
                  return dateKey(
                    new Date(d.getFullYear(), d.getMonth() + delta, 1, 12),
                  );
                });
              }
            }}
            onToday={() => setAnchor(todayKey())}
            onDimension={setDimension}
            onShowDetails={showCalendarDetails}
          />
        ) : (
          <div className={styles.scrollX}>
            <div className={styles.planList}>
              {visible.length ? (
                visible.map((p, i) => (
                  <article className={styles.planRow} key={p.id}>
                    <span className={styles.rank}>{i + 1}</span>
                    <div>
                      <span
                        className={cx(
                          styles.tag,
                          p.source === "分行关注" ? styles.red : styles.purple,
                        )}
                      >
                        {p.source}
                      </span>
                    </div>
                    <div>
                      <div className={styles.planTitle}>{p.name}</div>
                      <p className={styles.planDesc}>{p.desc}</p>
                      <div className={styles.planDate}>
                        <Icon name="calendar" />
                        {p.period}（{p.start.slice(5).replace("-", ".")} -{" "}
                        {p.end.slice(5).replace("-", ".")}）
                        <span className={styles.planFrequency}>
                          {planFrequency(p)}
                        </span>
                      </div>
                    </div>
                    <div className={styles.metric} aria-busy={statsLoading}>
                      <small>目标客户</small>
                      <strong>{customersText(p)}</strong>
                    </div>
                    <div className={styles.metric} aria-busy={statsLoading}>
                      <small>已生成任务</small>
                      <strong>{tasksText(p)}</strong>
                    </div>
                    {/* 恢复后，同步修改index.module.less中的样式 */}
                    {/* <div className={styles.metric}>
                      <small>任务执行率</small>
                      <div className={styles.progressLine}>
                        <div className={styles.progress}>
                          <span style={{ width: `${p.rate}%` }}></span>
                        </div>
                        <b>{p.rate}%</b>
                      </div>
                    </div> */}
                    <div className={styles.metric}>
                      <small>状态</small>
                      <span
                        className={cx(
                          styles.status,
                          statusClass(planPublishLabel(p)),
                        )}
                      >
                        <i className={styles.dot}></i>
                        {planPublishLabel(p)}
                      </span>
                    </div>
                    <div className={styles.actions}>
                      {p.editable !== false ? (
                        <>
                          <button
                            className={`${styles.btn} ${styles.sm} ${styles.soft}`}
                            onClick={() => onEditPlan(p.id)}
                          >
                            修改规划
                          </button>
                          <button
                            className={`${styles.btn} ${styles.sm}`}
                            onClick={() => onRemovePlan(p.id)}
                          >
                            移除规划
                          </button>
                        </>
                      ) : (
                        <span className={styles.readonlyHint}>只读</span>
                      )}
                    </div>
                  </article>
                ))
              ) : (
                <div className={styles.empty}>当前筛选条件下暂无规划</div>
              )}
            </div>
          </div>
        )}
      </section>
    </>
  );
}
