import { createStyles } from "antd-style";

export type HistoryLoadState = "idle" | "loading" | "error" | "exhausted";

interface HistoryLoadStatusProps {
  state: HistoryLoadState;
  retrying?: boolean;
  onRetry: () => void;
  onLoadMore?: () => void;
  advancing?: boolean;
  limitReached?: boolean;
}

const useStyles = createStyles(({ css, token }) => ({
  root: css`
    display: flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    min-height: 36px;
    color: ${token.colorTextTertiary};
    font-size: 12px;
    line-height: 20px;
  `,
  surface: css`
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-height: 28px;
    padding: 3px 8px;
    border: 1px solid transparent;
    border-radius: 8px;
  `,
  errorSurface: css`
    border-color: ${token.colorErrorBorder};
    background: ${token.colorErrorBg};
    color: ${token.colorText};
  `,
  errorIcon: css`
    flex: none;
    color: ${token.colorError};
    font-size: 14px;
  `,
  spinner: css`
    width: 12px;
    height: 12px;
    flex: none;
    border: 1.5px solid ${token.colorBorder};
    border-top-color: ${token.colorPrimary};
    border-radius: 50%;
    animation: history-status-spin 700ms linear infinite;

    @keyframes history-status-spin {
      to {
        transform: rotate(360deg);
      }
    }

    @media (prefers-reduced-motion: reduce) {
      animation: none;
      border-right-color: ${token.colorPrimary};
    }
  `,
  retry: css`
    min-height: 22px;
    margin-left: 2px;
    padding: 0 6px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: ${token.colorText};
    font: inherit;
    font-weight: 500;
    cursor: pointer;
    transition:
      color ${token.motionDurationMid},
      background-color ${token.motionDurationMid};

    &:hover:not(:disabled) {
      background: ${token.colorErrorBgHover};
      color: ${token.colorText};
    }

    &:focus-visible {
      outline: 2px solid ${token.colorPrimaryBorder};
      outline-offset: 1px;
    }

    &:disabled {
      cursor: default;
      opacity: 0.55;
    }

    @media (prefers-reduced-motion: reduce) {
      transition: none;
    }
  `,
}));

export default function HistoryLoadStatus({
  state,
  retrying = false,
  onRetry,
  onLoadMore,
  advancing = false,
  limitReached = false,
}: HistoryLoadStatusProps) {
  const { cx, styles } = useStyles();
  const isRetrying = state === "loading" && retrying;
  const isErrorSurface = state === "error" || isRetrying;

  return (
    <div
      aria-live="polite"
      className={styles.root}
      data-state={state}
      role={isErrorSurface ? "alert" : "status"}
    >
      {state === "idle" ? (
        onLoadMore ? (
          <button
            className={styles.retry}
            disabled={advancing}
            onClick={onLoadMore}
            type="button"
          >
            {limitReached ? "本轮内容较多，继续加载更早消息" : "加载更早消息"}
          </button>
        ) : null
      ) : (
        <div
          className={cx(styles.surface, {
            [styles.errorSurface]: isErrorSurface,
          })}
        >
          {isErrorSurface ? (
            isRetrying ? (
              <span aria-hidden="true" className={styles.spinner} />
            ) : (
              <svg
                aria-hidden="true"
                className={styles.errorIcon}
                fill="none"
                height="14"
                viewBox="0 0 16 16"
                width="14"
              >
                <circle cx="8" cy="8" fill="currentColor" r="7" />
                <path
                  d="M8 4.5v4M8 11.5h.01"
                  stroke="white"
                  strokeLinecap="round"
                  strokeWidth="1.5"
                />
              </svg>
            )
          ) : state === "loading" ? (
            <span aria-hidden="true" className={styles.spinner} />
          ) : null}
          <span>
            {isRetrying
              ? "正在重新加载历史消息…"
              : state === "loading"
              ? "正在加载更早的消息…"
              : state === "error"
              ? "历史消息加载失败"
              : "已到达会话开始处"}
          </span>
          {isErrorSurface ? (
            <button
              aria-label={
                isRetrying ? "正在重试加载历史消息" : "重试加载历史消息"
              }
              className={styles.retry}
              disabled={isRetrying}
              onClick={onRetry}
              type="button"
            >
              {isRetrying ? "重试中" : "重试"}
            </button>
          ) : null}
        </div>
      )}
    </div>
  );
}
