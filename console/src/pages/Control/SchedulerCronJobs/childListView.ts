import type { ManagedChild } from "@/api/types/schedulerCron";

type ChildProfile = Pick<ManagedChild, "tenant_id" | "tenant_name" | "bbk_id">;
const text = (value?: string | null) => (value || "").trim();

/** 搜索与机构取交集，始终基于已完整读取的列表，而非当前表格页。 */
export function filterChildren<T extends ChildProfile>(
  items: readonly T[],
  search: string,
  branch: string,
): T[] {
  const keyword = search.trim().toLowerCase();
  return items.filter(
    (item) =>
      (!branch || text(item.bbk_id) === branch) &&
      (!keyword ||
        [item.tenant_name, item.tenant_id].some((value) =>
          text(value).toLowerCase().includes(keyword),
        )),
  );
}

export function childBbkOptions(items: readonly ChildProfile[]) {
  return [...new Set(items.map((item) => text(item.bbk_id)).filter(Boolean))]
    .sort((left, right) =>
      left.localeCompare(right, "zh-CN", { numeric: true }),
    )
    .map((value) => ({ label: value, value }));
}

/** 同一用户可有多个Agent，提示中的人数按不同UID计算。 */
export function duplicateChildNames(items: readonly ChildProfile[]) {
  const usersByName = new Map<string, Set<string>>();
  for (const item of items) {
    const name = text(item.tenant_name);
    if (!name) continue;
    const users = usersByName.get(name) || new Set<string>();
    users.add(item.tenant_id);
    usersByName.set(name, users);
  }
  return [...usersByName]
    .filter(([, users]) => users.size > 1)
    .map(([name, users]) => `${name} (${users.size} 个 UID)`);
}
