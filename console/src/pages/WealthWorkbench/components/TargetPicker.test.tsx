import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useWealthStore } from "../store";
import { TargetPicker } from "./TargetPicker";

vi.mock("../store", async () => {
  const { create } = await import("zustand");
  return {
    useWealthStore: create((set) => ({
      targets: [],
      targetsLoaded: true,
      targetSapIds: [],
      loadTargets: vi.fn(),
      toggleTarget: (sapId: string) =>
        set((state: { targetSapIds: string[] }) => ({
          targetSapIds: state.targetSapIds.includes(sapId)
            ? state.targetSapIds.filter((id) => id !== sapId)
            : [...state.targetSapIds, sapId],
        })),
    })),
  };
});

describe("TargetPicker 全选", () => {
  afterEach(cleanup);
  beforeEach(() => {
    useWealthStore.setState({
      targets: [
        { sapId: "1", name: "张三", orgName: "机构A" },
        { sapId: "2", name: "李四", orgName: "机构B" },
        { sapId: "3", name: "王五", orgName: "机构B" },
      ],
      targetsLoaded: true,
      targetSapIds: ["1"],
    });
  });

  it("保留已有选择并全选，支持取消全选和再次全选", () => {
    render(<TargetPicker />);
    fireEvent.click(screen.getByRole("button", { name: "全选" }));

    expect(useWealthStore.getState().targetSapIds).toEqual(["1", "2", "3"]);
    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(useWealthStore.getState().targetSapIds).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    expect(useWealthStore.getState().targetSapIds).toEqual(["1", "2", "3"]);

    fireEvent.click(screen.getByRole("button", { name: "移除李四" }));
    expect(screen.getByRole("button", { name: "全选" })).toBeEnabled();
  });

  it("只全选搜索结果，同时保留搜索范围外的已选人员", () => {
    render(<TargetPicker />);
    fireEvent.change(screen.getByRole("textbox", { name: "搜索分发目标" }), {
      target: { value: "李四" },
    });
    fireEvent.click(screen.getByRole("button", { name: "全选搜索结果" }));

    expect(useWealthStore.getState().targetSapIds).toEqual(["1", "2"]);
    fireEvent.click(screen.getByRole("button", { name: "取消全选搜索结果" }));
    expect(useWealthStore.getState().targetSapIds).toEqual(["1"]);
    expect(screen.getByRole("button", { name: "全选搜索结果" })).toBeEnabled();
  });

  it("加载中或无匹配结果时禁用全选", () => {
    useWealthStore.setState({ targetsLoaded: false });
    const { rerender } = render(<TargetPicker />);
    expect(screen.getByRole("button", { name: "全选" })).toBeDisabled();

    useWealthStore.setState({ targetsLoaded: true });
    rerender(<TargetPicker />);
    fireEvent.change(screen.getByRole("textbox", { name: "搜索分发目标" }), {
      target: { value: "不存在" },
    });
    expect(screen.getByRole("button", { name: "全选搜索结果" })).toBeDisabled();
  });
});
