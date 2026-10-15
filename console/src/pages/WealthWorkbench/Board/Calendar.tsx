/**
 * 智能财富工作台 —— 规划看板·日历视图
 * 对应原型 calendarHTML：月/周双维度、排程事件卡片、今日高亮。
 */
import { useEffect, useRef, useState } from "react";
import cx from "classnames";
import { Tooltip } from "antd";
import styles from "../index.module.less";
import type { Plan } from "../types";
import {
  addCalendarDays,
  calendarDate,
  calendarRange,
  planFrequency,
  planPublishLabel,
  planScheduledOn,
  todayKey,
} from "../utils";
import { Icon } from "../components/Icon";

const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

export function Calendar({
  sources,
  visible,
  dimension,
  anchor,
  onShift,
  onToday,
  onDimension,
  onShowDetails,
  planStat,
}: {
  sources: readonly string[];
  visible: Plan[];
  dimension: "week" | "month";
  anchor: string;
  onShift: (delta: number) => void;
  onToday: () => void;
  onDimension: (d: "week" | "month") => void;
  onShowDetails: (day: string, id?: string) => void;
  planStat: (plan: Plan) => { customers: number; tasks: number } | null;
}) {
  const todayCellRef = useRef<HTMLDivElement>(null);
  const [shouldFocusToday, setShouldFocusToday] = useState(false);
  const [start, end] = calendarRange(anchor, dimension);
  const first =
    dimension === "month"
      ? addCalendarDays(start, -((calendarDate(start).getDay() + 6) % 7))
      : start;
  const last =
    dimension === "month"
      ? addCalendarDays(end, 6 - ((calendarDate(end).getDay() + 6) % 7))
      : end;
  const days: string[] = [];
  for (let day = first; day <= last; day = addCalendarDays(day, 1))
    days.push(day);
  const month = calendarDate(anchor);
  const todayKeyStr = todayKey();
  const heading =
    dimension === "month"
      ? `${month.getFullYear()}年 ${month.getMonth() + 1}月`
      : `${start.replace(/-/g, ".")} — ${end.slice(5).replace("-", ".")}`;

  useEffect(() => {
    if (!shouldFocusToday) return;

    if (dimension === "month") {
      const todayCell = todayCellRef.current;
      if (todayCell) {
        const { top, bottom } = todayCell.getBoundingClientRect();
        const isVisible = top >= 0 && bottom <= window.innerHeight;
        if (!isVisible) {
          todayCell.scrollIntoView({
            behavior: "smooth",
            block: "center",
            inline: "nearest",
          });
        }
      }
    }

    setShouldFocusToday(false);
  }, [anchor, dimension, shouldFocusToday]);

  const handleToday = () => {
    setShouldFocusToday(true);
    onToday();
  };

  return (
    <>
      <div className={styles.calendarToolbar}>
        <div className={styles.calendarNavigation}>
          <button
            className={styles.iconbtn}
            aria-label={dimension === "month" ? "上月" : "上周"}
            onClick={() => onShift(-1)}
          >
            <Icon name="left" />
          </button>
          <strong>{heading}</strong>
          <button
            className={styles.iconbtn}
            aria-label={dimension === "month" ? "下月" : "下周"}
            onClick={() => onShift(1)}
          >
            <Icon name="right" />
          </button>
          <button
            className={`${styles.btn} ${styles.sm}`}
            onClick={handleToday}
          >
            今天
          </button>
        </div>
        <div className={styles.calendarControls}>
          <div className={styles.calendarLegend}>
            <span>
              <i></i>分行关注
            </span>
            {sources.some((source) => source !== "分行关注") && (
              <span>
                <i className={styles.purple}></i>
                {sources.includes("我的关注") ? "行长 / 我的关注" : "行长关注"}
              </span>
            )}
          </div>
          <div className={styles.switch} aria-label="日历维度">
            <button
              className={dimension === "week" ? styles.active : ""}
              aria-pressed={dimension === "week"}
              onClick={() => onDimension("week")}
            >
              周
            </button>
            <button
              className={dimension === "month" ? styles.active : ""}
              aria-pressed={dimension === "month"}
              onClick={() => onDimension("month")}
            >
              月
            </button>
          </div>
        </div>
      </div>
      <div className={styles.calendarScroll}>
        <div
          className={cx(styles.calendarGrid, styles[dimension])}
          aria-label={`${heading}工作任务日历`}
        >
          {WEEKDAYS.map((w) => (
            <div className={styles.calendarWeekday} key={w}>
              {w}
            </div>
          ))}
          {days.map((day) => {
            const inRange = day >= start && day <= end;
            const items = inRange
              ? visible.filter((p) => planScheduledOn(p, day))
              : [];
            const today = day === todayKeyStr;
            const weekday = calendarDate(day).getDay();
            const shown = dimension === "month" ? items.slice(0, 3) : items;
            return (
              <div
                key={day}
                ref={today ? todayCellRef : undefined}
                className={cx(
                  styles.calendarCell,
                  !inRange && styles.outside,
                  today && styles.isToday,
                  (weekday === 0 || weekday === 6) && styles.weekend,
                )}
                data-date={day}
              >
                <div className={styles.calendarDateLine}>
                  <button
                    className={cx(styles.calendarDay, today && styles.today)}
                    aria-label={
                      day +
                      (items.length
                        ? `，查看${items.length}项任务详情`
                        : "，暂无工作任务")
                    }
                    disabled={!inRange}
                    onClick={() => onShowDetails(day)}
                  >
                    {Number(day.slice(8))}
                  </button>
                  <span className={styles.calendarDayCount}>
                    {today ? "今天" : items.length ? `${items.length} 项` : ""}
                  </span>
                </div>
                {shown.map((p) => {
                  const stat = planStat(p);
                  const sceneNames =
                    (p.items ?? [])
                      .map((item) => item.sceneName)
                      .filter(Boolean)
                      .join("，") || "未配置";
                  return (
                    <Tooltip
                      key={p.id}
                      placement="topLeft"
                      trigger={["hover", "focus"]}
                      title={
                        <div className={styles.calendarEventTooltip}>
                          <div>规划名称：{p.name}</div>
                          <div>经营场景：{sceneNames}</div>
                          <div>{planPublishLabel(p)} · 点击查看详情</div>
                        </div>
                      }
                    >
                      <button
                        className={cx(
                          styles.calendarEvent,
                          p.source !== "分行关注" && styles.purple,
                        )}
                        aria-label={`${p.name}，${
                          p.items?.length ?? 0
                        } 个技能，点击查看详情`}
                        onClick={() => onShowDetails(day, p.id)}
                      >
                        <span className={styles.calendarEventTitle}>
                          {dimension === "month" ? (
                            <span className={styles.calendarPlanIdentity}>
                              <strong>{p.name}</strong>
                              <span
                                className={styles.calendarSkillCount}
                                aria-label={`${p.items?.length ?? 0} 个技能`}
                              >
                                {p.items?.length ?? 0}
                              </span>
                            </span>
                          ) : (
                            <strong>{p.name}</strong>
                          )}
                          <span className={styles.calendarFrequency}>
                            {planFrequency(p)}
                          </span>
                        </span>
                        {dimension === "week" && (
                          <span className={styles.calendarEventScenes}>
                            经营场景：{sceneNames}
                          </span>
                        )}
                        {dimension === "week" && (
                          <small>
                            {p.source} · {stat?.customers ?? "--"} 位目标客户
                            <br />
                            已生成 {stat?.tasks ?? "--"} 项任务
                            <br />
                            {planPublishLabel(p)}
                          </small>
                        )}
                      </button>
                    </Tooltip>
                  );
                })}
                {dimension === "month" && items.length > 3 && (
                  <button
                    className={styles.calendarMore}
                    onClick={() => onShowDetails(day)}
                  >
                    +{items.length - 3} 项 · 查看全部
                  </button>
                )}
                {inRange && !items.length && (
                  <div className={styles.calendarEmpty}>暂无工作任务</div>
                )}
              </div>
            );
          })}
        </div>
      </div>
      <div className={styles.calendarNote}>
        <span>
          按各场景的任务周期和执行频率展示；卡片数量与执行率为整项规划累计数据。
        </span>
        <span>点击任务或日期查看详情</span>
      </div>
    </>
  );
}
