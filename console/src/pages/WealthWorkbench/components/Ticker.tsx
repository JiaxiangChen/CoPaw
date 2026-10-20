/**
 * 智能财富工作台 —— 信息播报滚动条
 * 悬停播报文字或手动暂停时停止滚动，prefers-reduced-motion 时静态展示。
 */
import { useState } from "react";
import cx from "classnames";
import styles from "../index.module.less";
import { useWealthStore } from "../store";
import { Icon } from "./Icon";

function TickerGroup({ hidden }: { hidden?: boolean }) {
  return (
    <div className={styles.tickerGroup} aria-hidden={hidden || undefined}>
      {/* <span className={styles.tickerItem}>
        <span className={styles.tickerCategory}>市场行情</span>
        <span>
          上证指数 <b>3,258.76</b> <b className={styles.marketUp}>+0.62%</b>
          {"\u3000·\u3000沪深300 "}
          <b>3,826.45</b> <b className={styles.marketUp}>+0.48%</b>
        </span>
      </span> */}
      <span className={styles.tickerItem}>
        <span className={styles.tickerCategory}>财经信息</span>
        <span>
          {
            "关注利率变化与资产配置动态　·　本周关注：养老金融、保险保障与到期资金承接"
          }
        </span>
      </span>
    </div>
  );
}

export function Ticker() {
  const [paused, setPaused] = useState(false);
  const openDialog = useWealthStore((s) => s.openDialog);

  const showTickerDetails = () => {
    openDialog({
      title: "信息播报",
      body: (
        <>
          <div className={styles.recommendation}>
            <h3>市场行情</h3>
            <p>
              上证指数：3,258.76（+0.62%）
              <br />
              沪深300：3,826.45（+0.48%）
            </p>
          </div>
          <div className={styles.recommendation}>
            <h3>财经信息</h3>
            <p>
              关注利率变化与资产配置动态。
              <br />
              本周关注：养老金融、保险保障与到期资金承接。
            </p>
          </div>
          {/* <p className={styles.pageNote}>
            当前内容为原型示例数据，未接入实时行情或资讯系统。
          </p> */}
        </>
      ),
      buttons: [{ label: "关闭" }],
    });
  };

  return (
    <section
      className={cx(styles.wealthTicker, paused && styles.paused)}
      aria-label="信息播报：市场行情和财经信息，当前为示例数据"
    >
      <div className={styles.tickerLabel}>
        <Icon name="megaphone" />
        <span>信息播报</span>
      </div>
      <div className={styles.tickerWindow}>
        <div className={styles.tickerTrack}>
          <TickerGroup />
          <TickerGroup hidden />
        </div>
      </div>
      <div className={styles.tickerActions}>
        {/* <span className={styles.tickerDemo}>示例数据</span> */}
        <button
          className={styles.tickerControl}
          aria-label={paused ? "继续自动播报" : "暂停自动播报"}
          aria-pressed={paused}
          onClick={() => setPaused((v) => !v)}
        >
          {paused ? "继续" : "暂停"}
        </button>
        <button className={styles.tickerControl} onClick={showTickerDetails}>
          查看全部
        </button>
      </div>
    </section>
  );
}
