import dayjs from "dayjs";
import { Tag } from "antd";

export const labels: Record<string, string> = {
  received: "已接收",
  pending: "等待处理",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  queued: "排队中",
  dispatching: "派发中",
  accepted: "已受理",
  success: "主执行成功",
  error: "执行失败",
  dispatch_failed: "派发未确认",
  skipped: "已跳过",
  cancelled: "已取消",
};
export function statusTag(status: string) {
  const colors: Record<string, string> = {
    error: "error",
    failed: "error",
    success: "success",
    completed: "success",
    queued: "processing",
    pending: "processing",
    accepted: "geekblue",
    dispatching: "blue",
    running: "blue",
    dispatch_failed: "warning",
    cancelled: "warning",
  };
  const color = colors[status] || "default";
  return <Tag color={color}>{labels[status] || status}</Tag>;
}
export const timeText = (value?: string | null) =>
  value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "-";
