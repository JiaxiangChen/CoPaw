import { useEffect, useId, useRef, useState } from "react";
import cx from "classnames";
import styles from "../index.module.less";
import type { Customer } from "../types";
import {
  CONTACT_STATUS_OPTIONS,
  contactStatusOf,
  type ContactStatusFilterValue,
} from "./contactStatus";

const FILTER_LABEL = "筛选接触状态";

export function ContactStatusFilter({
  value,
  customers,
  onChange,
}: {
  value: ContactStatusFilterValue;
  customers: Customer[];
  onChange: (value: ContactStatusFilterValue) => void;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 10, top: 10 });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const id = useId();
  const active = value !== "全部";

  useEffect(() => {
    if (!open) return;
    popoverRef.current
      ?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')
      ?.focus();
    const closeOutside = (event: MouseEvent) => {
      if (
        !triggerRef.current?.contains(event.target as Node) &&
        !popoverRef.current?.contains(event.target as Node)
      )
        setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    };
    const close = () => setOpen(false);
    document.addEventListener("click", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("click", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  const apply = (next: ContactStatusFilterValue) => {
    setOpen(false);
    onChange(next);
    triggerRef.current?.focus();
  };
  const toggle = () => {
    const rect = triggerRef.current!.getBoundingClientRect();
    setPosition({
      left: Math.max(10, Math.min(rect.left, window.innerWidth - 240)),
      top:
        rect.bottom + 242 > window.innerHeight - 10
          ? Math.max(10, rect.top - 242)
          : rect.bottom + 8,
    });
    setOpen(!open);
  };

  return (
    <>
      <button
        type="button"
        ref={triggerRef}
        className={cx(styles.tagFilterTrigger, active && styles.filtered)}
        aria-label={FILTER_LABEL + (active ? `，当前：${value}` : "")}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={id}
        onClick={toggle}
      >
        <span>接触状态</span>
        <span className={styles.tagFilterIcon}>
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <path d="M3 4h14l-5.5 6v5l-3 1v-6Z" />
          </svg>
          {active && <i />}
        </span>
      </button>
      {active && (
        <div className={styles.tagFilterApplied}>
          <span>{value}</span>
          <button
            type="button"
            aria-label="清除接触状态筛选"
            title="清除筛选"
            onClick={() => apply("全部")}
          >
            ×
          </button>
        </div>
      )}
      {open && (
        <div
          id={id}
          ref={popoverRef}
          role="dialog"
          aria-label={FILTER_LABEL}
          className={styles.tagFilterPopover}
          style={position}
          onKeyDown={(event) => {
            if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key))
              return;
            const choices = Array.from(
              popoverRef.current!.querySelectorAll<HTMLButtonElement>("button"),
            );
            const index = choices.indexOf(
              document.activeElement as HTMLButtonElement,
            );
            const next =
              event.key === "Home"
                ? 0
                : event.key === "End"
                ? choices.length - 1
                : (index +
                    (event.key === "ArrowDown" ? 1 : -1) +
                    choices.length) %
                  choices.length;
            event.preventDefault();
            choices[next]?.focus();
          }}
        >
          <div className={styles.tagFilterPopoverTitle}>
            {FILTER_LABEL}
            <span>单选</span>
          </div>
          {CONTACT_STATUS_OPTIONS.map((option) => (
            <button
              type="button"
              key={option}
              className={cx(
                styles.tagFilterChoice,
                value === option && styles.selected,
              )}
              aria-pressed={value === option}
              onClick={() => apply(option)}
            >
              <span
                className={
                  option === "全部"
                    ? styles.allTagText
                    : cx(
                        styles.tag,
                        option === "已接触" ? styles.green : styles.emptyTag,
                      )
                }
              >
                {option === "全部" ? "全部状态" : option}
              </span>
              <span className={styles.tagFilterChoiceCount}>
                {option === "全部"
                  ? customers.length
                  : customers.filter(
                      (customer) =>
                        contactStatusOf(customer.touched) === option,
                    ).length}
              </span>
              <span className={styles.tagFilterChoiceCheck} aria-hidden="true">
                {value === option ? "✓" : ""}
              </span>
            </button>
          ))}
        </div>
      )}
    </>
  );
}
