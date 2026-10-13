import type { Customer } from "../types";

export const CONTACT_STATUS_OPTIONS = [
  "全部",
  "未接触",
  "已接触",
  "--",
] as const;
export type ContactStatusFilterValue = (typeof CONTACT_STATUS_OPTIONS)[number];

export function contactStatusOf(touched: Customer["touched"]) {
  return touched === 1 ? "已接触" : touched === 0 ? "未接触" : "--";
}
