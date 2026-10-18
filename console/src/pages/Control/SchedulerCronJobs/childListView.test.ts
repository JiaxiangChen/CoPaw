import { describe, expect, it } from "vitest";
import {
  childBbkOptions,
  duplicateChildNames,
  filterChildren,
} from "./childListView";

const items = [
  { tenant_id: "Alice", tenant_name: "张三", bbk_id: "755" },
  { tenant_id: "bob", tenant_name: "张三", bbk_id: "110" },
  { tenant_id: "bob", tenant_name: "张三", bbk_id: "110" },
  { tenant_id: "carol", tenant_name: null, bbk_id: null },
];
describe("新版子任务列表视图", () => {
  it("姓名或UID忽略大小写搜索，并与机构取交集", () => {
    expect(filterChildren(items, "  ALICE  ", "755")).toEqual([items[0]]);
    expect(filterChildren(items, "张三", "110")).toEqual([items[1], items[2]]);
    expect(filterChildren(items, "ALICE", "110")).toEqual([]);
    expect(filterChildren(items, "CAROL", "")).toEqual([items[3]]);
    expect(items).toHaveLength(4);
  });
  it("机构选项来自完整结果、去重排序，不把空资料作为机构", () => {
    expect(childBbkOptions(items)).toEqual([
      { label: "110", value: "110" },
      { label: "755", value: "755" },
    ]);
  });
  it("同名按不同UID计数，同一UID多Agent不会重复报警", () => {
    expect(duplicateChildNames(items)).toEqual(["张三 (2 个 UID)"]);
    expect(duplicateChildNames([items[1], items[2]])).toEqual([]);
  });
});
