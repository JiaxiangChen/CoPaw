/**
 * 智能财富工作台 —— Toast 提示
 * 对应原型 #toast：显示 3.2s 后自动消失。
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import cx from "classnames";
import styles from "../index.module.less";
import { useWealthStore } from "../store";

export function Toast({ container }: { container?: HTMLElement | null }) {
  const toastText = useWealthStore((s) => s.toastText);
  const toastSeq = useWealthStore((s) => s.toastSeq);
  const [visible, setVisible] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!toastSeq) return;
    setVisible(true);
    const timer = setTimeout(() => setVisible(false), 3200);
    return () => clearTimeout(timer);
  }, [toastSeq]);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    // 旧内核通过现有样式显示提示，不设置会让元素默认隐藏的 popover 属性。
    if (
      typeof element.showPopover !== "function" ||
      typeof element.hidePopover !== "function"
    ) {
      return;
    }
    element.setAttribute("popover", "manual");
    if (visible) element.showPopover();
    else element.hidePopover();
  }, [visible, container]);

  const content = (
    <div
      ref={ref}
      hidden={!visible}
      className={cx(styles.toast, visible && styles.visible)}
      role="status"
      aria-live="polite"
    >
      {toastText}
    </div>
  );
  return container ? createPortal(content, container) : content;
}
