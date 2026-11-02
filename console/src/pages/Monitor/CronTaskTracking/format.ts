import dayjs from "dayjs";
import utc from "dayjs/plugin/utc";

dayjs.extend(utc);
export const entries: Record<string, string> = {
  all: "全部入口",
  ordinary: "普通调度",
  batch: "批调度",
  manual: "手动执行",
  manual_retry: "人工重试",
  daily_failure_retry: "失败补跑",
  unknown: "入口未确认",
  management: "管理请求",
};
export const categories = [
  { value: "all", label: "全部节点" },
  { value: "management", label: "管理操作" },
  { value: "scheduling", label: "调度过程" },
  { value: "execution", label: "执行结果" },
  { value: "notification", label: "通知与标读" },
];
export const statuses: Record<string, string> = {
  success: "成功",
  error: "失败",
  failed: "失败",
  completed: "已完成",
  ready: "就绪",
  pending: "待处理",
  running: "处理中",
  queued: "待派发",
  accepted: "已受理",
  skipped: "已跳过",
  cancelled: "已取消",
  sent: "已发送",
  read: "已标读",
  ordinary: "普通调度",
  batch_enabled: "开启批调度",
  batch_paused: "暂停批调度",
};
export function text(value: unknown): string {
  if (value === null || value === undefined || value === "") return "未记录";
  if (typeof value === "boolean") return value ? "是" : "否";
  return typeof value === "object"
    ? JSON.stringify(value, null, 2)
    : String(value);
}
export function time(value: unknown) {
  const parsed = typeof value === "string" ? dayjs(value) : null;
  return parsed?.isValid()
    ? parsed.utcOffset(480).format("YYYY-MM-DD HH:mm:ss")
    : "未记录";
}
export function color(value: unknown) {
  if (["error", "failed", "dispatch_failed"].includes(String(value)))
    return "error";
  if (["success", "completed", "sent", "read", "ready"].includes(String(value)))
    return "success";
  return "default";
}
