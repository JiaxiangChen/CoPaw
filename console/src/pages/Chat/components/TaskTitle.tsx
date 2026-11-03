import { useLayoutEffect, useRef, useState } from "react";
import { Tooltip } from "antd";

export default function TaskTitle({
  title,
  className,
}: {
  title: string;
  className: string;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const [overflowing, setOverflowing] = useState(false);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;

    const measure = () => {
      setOverflowing(element.scrollWidth > element.clientWidth);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [title]);

  return (
    <Tooltip title={overflowing ? title : null} placement="topLeft">
      <span ref={ref} className={className}>
        {title}
      </span>
    </Tooltip>
  );
}
