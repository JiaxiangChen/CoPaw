import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchSkillStats } from "../api";
import type { Plan, SkillStat } from "../types";
import { usePlanStats } from "./usePlanStats";

vi.mock("../api", () => ({ fetchSkillStats: vi.fn() }));

function plan(id: string): Plan {
  return {
    id,
    name: id,
    source: "我的关注",
    desc: "",
    period: "自定义",
    status: "已自动下发",
    start: "2026-10-01",
    end: "2026-10-31",
    customers: 0,
    tasks: 0,
    rate: 0,
    items: [
      {
        id,
        sceneName: id,
        categoryLabel: "理财",
        categoryCode: "finance",
        direction: "客户经营",
        cycle: "本月",
        start: "2026-10-01",
        end: "2026-10-31",
        mcpRelations: [],
        schedule: { type: "daily", hour: 9, minute: 0 },
      },
    ],
  };
}

function deferred() {
  let resolve!: (data: Record<string, SkillStat> | null) => void;
  const promise = new Promise<Record<string, SkillStat> | null>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("usePlanStats", () => {
  beforeEach(() =>
    vi.mocked(fetchSkillStats).mockReset().mockResolvedValue({}),
  );
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("按规划 ID 查询，相同参数、重排和重复规划不重新查询", async () => {
    const plans = [plan("a"), plan("b")];
    const { result, rerender } = renderHook(
      ({ visible }) => usePlanStats(visible, plans, "rm"),
      { initialProps: { visible: plans } },
    );
    await waitFor(() => expect(result.current.statsLoading).toBe(false));
    expect(fetchSkillStats).toHaveBeenCalledWith(
      ["a", "b"],
      expect.any(AbortSignal),
    );
    rerender({ visible: [plans[1], plans[0], plans[0]] });
    expect(fetchSkillStats).toHaveBeenCalledTimes(1);
    expect(result.current.statsLoading).toBe(false);
    rerender({ visible: [plans[0]] });
    await waitFor(() => expect(result.current.statsLoading).toBe(false));
    expect(fetchSkillStats).toHaveBeenCalledTimes(2);
    rerender({ visible: plans });
    expect(result.current.statsLoading).toBe(false);
    expect(fetchSkillStats).toHaveBeenCalledTimes(2);
  });

  it("快速切换周期合并在途请求，过期响应不会覆盖当前统计", async () => {
    const plans = [plan("a"), plan("b")];
    const first = deferred();
    const second = deferred();
    vi.mocked(fetchSkillStats)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const { result, rerender } = renderHook(
      ({ visible }) => usePlanStats(visible, plans, "rm"),
      { initialProps: { visible: [plans[0]] } },
    );
    rerender({ visible: [plans[1]] });
    rerender({ visible: [plans[0]] });
    expect(fetchSkillStats).toHaveBeenCalledTimes(2);
    expect(result.current.statsLoading).toBe(true);
    const current = { a: { targetCustomerCount: 12, generatedTaskCount: 3 } };
    await act(async () => first.resolve(current));
    expect(result.current.sceneStats).toEqual(current);
    await act(async () => second.resolve({}));
    expect(result.current.sceneStats).toEqual(current);
  });

  it("新查询加载时清除旧数字，失败保持占位而非零", async () => {
    const plans = [plan("a"), plan("b")];
    const pending = deferred();
    vi.mocked(fetchSkillStats)
      .mockResolvedValueOnce({
        a: { targetCustomerCount: 12, generatedTaskCount: 3 },
      })
      .mockReturnValueOnce(pending.promise);
    const { result, rerender } = renderHook(
      ({ visible }) => usePlanStats(visible, plans, "rm"),
      { initialProps: { visible: [plans[0]] } },
    );
    await waitFor(() => expect(result.current.statsLoading).toBe(false));
    rerender({ visible: [plans[1]] });
    expect(result.current).toMatchObject({
      sceneStats: null,
      statsLoading: true,
    });
    await act(async () => pending.resolve(null));
    expect(result.current).toMatchObject({
      sceneStats: null,
      statsLoading: false,
    });
  });

  it("规划轮询复用统计，切换账户使缓存失效", async () => {
    const plans = [plan("a")];
    const { result, rerender } = renderHook(
      ({ snapshot, account }) => usePlanStats(snapshot, snapshot, account),
      { initialProps: { snapshot: plans, account: "rm" } },
    );
    await waitFor(() => expect(result.current.statsLoading).toBe(false));
    rerender({ snapshot: [...plans], account: "rm" });
    await waitFor(() => expect(result.current.statsLoading).toBe(false));
    expect(fetchSkillStats).toHaveBeenCalledTimes(1);
    rerender({ snapshot: plans, account: "president" });
    await waitFor(() => expect(result.current.statsLoading).toBe(false));
    expect(fetchSkillStats).toHaveBeenCalledTimes(2);
  });

  it("空查询立即展示空统计且不调用接口", () => {
    const { result } = renderHook(() => usePlanStats([], [], "rm"));
    expect(result.current).toMatchObject({
      sceneStats: {},
      statsLoading: false,
    });
    expect(fetchSkillStats).not.toHaveBeenCalled();
  });

  it("30 秒内复用，过期后相同周期参数重新查询，无关重渲染不自动刷新", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(100_000);
    const plans = [plan("a")];
    const pending = deferred();
    vi.mocked(fetchSkillStats)
      .mockResolvedValueOnce({})
      .mockReturnValueOnce(pending.promise);
    const { result, rerender } = renderHook(
      ({ visible }) => usePlanStats(visible, plans, "rm"),
      { initialProps: { visible: plans } },
    );
    await waitFor(() => expect(result.current.statsLoading).toBe(false));
    now.mockReturnValue(129_999);
    const visible = [...plans];
    rerender({ visible });
    expect(fetchSkillStats).toHaveBeenCalledTimes(1);
    now.mockReturnValue(130_000);
    rerender({ visible });
    expect(fetchSkillStats).toHaveBeenCalledTimes(1);
    expect(result.current.statsLoading).toBe(false);
    rerender({ visible: [...plans] });
    expect(fetchSkillStats).toHaveBeenCalledTimes(2);
    expect(result.current).toMatchObject({
      sceneStats: {},
      statsLoading: false,
      statsUpdating: true,
    });
    const stats = { a: { targetCustomerCount: 20, generatedTaskCount: 8 } };
    await act(async () => pending.resolve(stats));
    expect(result.current.sceneStats).toEqual(stats);
    expect(result.current.statsLoading).toBe(false);
  });

  it("失败结果也会在 30 秒后重试", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(100_000);
    const plans = [plan("a")];
    vi.mocked(fetchSkillStats)
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce({});
    const { result, rerender } = renderHook(
      ({ visible }) => usePlanStats(visible, plans, "rm"),
      { initialProps: { visible: plans } },
    );
    await waitFor(() => expect(result.current.statsLoading).toBe(false));
    expect(result.current.sceneStats).toBeNull();
    now.mockReturnValue(129_999);
    rerender({ visible: [...plans] });
    expect(fetchSkillStats).toHaveBeenCalledTimes(1);
    now.mockReturnValue(130_000);
    rerender({ visible: [...plans] });
    await waitFor(() => expect(result.current.sceneStats).toEqual({}));
    expect(fetchSkillStats).toHaveBeenCalledTimes(2);
  });

  it("请求超过 30 秒仍共用，有效期从返回时计算", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(100_000);
    const plans = [plan("a")];
    const pending = deferred();
    vi.mocked(fetchSkillStats).mockReturnValueOnce(pending.promise);
    const { result, rerender } = renderHook(
      ({ visible }) => usePlanStats(visible, plans, "rm"),
      { initialProps: { visible: plans } },
    );
    now.mockReturnValue(140_000);
    rerender({ visible: [...plans] });
    expect(fetchSkillStats).toHaveBeenCalledTimes(1);
    expect(result.current.statsLoading).toBe(true);
    await act(async () => pending.resolve({}));
    now.mockReturnValue(169_999);
    rerender({ visible: [...plans] });
    expect(result.current.statsLoading).toBe(false);
    expect(fetchSkillStats).toHaveBeenCalledTimes(1);
  });
  it("慢统计接口期间连续规划轮询不清空数字或重复请求", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(100_000);
    const plans = [plan("a")];
    const oldStats = { a: { targetCustomerCount: 12, generatedTaskCount: 3 } };
    const pending = deferred();
    vi.mocked(fetchSkillStats)
      .mockResolvedValueOnce(oldStats)
      .mockReturnValueOnce(pending.promise);
    const { result, rerender } = renderHook(
      ({ snapshot }) => usePlanStats(snapshot, snapshot, "rm"),
      { initialProps: { snapshot: plans } },
    );
    await waitFor(() => expect(result.current.sceneStats).toEqual(oldStats));
    now.mockReturnValue(130_000);
    rerender({ snapshot: [...plans] });
    for (let tick = 1; tick <= 3; tick++) {
      now.mockReturnValue(130_000 + tick * 3_000);
      rerender({ snapshot: [...plans] });
      expect(result.current).toMatchObject({
        sceneStats: oldStats,
        statsLoading: false,
        statsUpdating: true,
      });
    }
    expect(fetchSkillStats).toHaveBeenCalledTimes(2);
    const latest = { a: { targetCustomerCount: 24, generatedTaskCount: 6 } };
    await act(async () => pending.resolve(latest));
    expect(result.current).toMatchObject({
      sceneStats: latest,
      statsUpdating: false,
    });
  });

  it("后台更新失败保留上次数字并报告失败", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(100_000);
    const plans = [plan("a")];
    const stats = { a: { targetCustomerCount: 12, generatedTaskCount: 3 } };
    vi.mocked(fetchSkillStats)
      .mockResolvedValueOnce(stats)
      .mockResolvedValueOnce(null);
    const { result, rerender } = renderHook(
      ({ visible }) => usePlanStats(visible, plans, "rm"),
      { initialProps: { visible: plans } },
    );
    await waitFor(() => expect(result.current.sceneStats).toEqual(stats));
    now.mockReturnValue(130_000);
    rerender({ visible: [...plans] });
    await waitFor(() => expect(result.current.statsFailed).toBe(true));
    expect(result.current).toMatchObject({
      sceneStats: stats,
      statsLoading: false,
      statsUpdating: false,
    });
  });

  it.each(["发布中", "分发中"])(
    "%s 完成后在有效期内主动刷新一次统计",
    async (status) => {
      const plans = [{ ...plan("a"), status }];
      const { result, rerender } = renderHook(
        ({ snapshot }) => usePlanStats(snapshot, snapshot, "rm"),
        { initialProps: { snapshot: plans } },
      );
      await waitFor(() => expect(result.current.statsLoading).toBe(false));
      rerender({ snapshot: [{ ...plans[0], status: "已自动下发" }] });
      await waitFor(() => expect(fetchSkillStats).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(result.current.statsUpdating).toBe(false));
      rerender({ snapshot: [{ ...plans[0], status: "已自动下发" }] });
      expect(fetchSkillStats).toHaveBeenCalledTimes(2);
    },
  );

  it("完成事件发生在请求途中时，串行补查完成后的统计", async () => {
    const plans = [{ ...plan("a"), status: "发布中" }];
    const first = deferred();
    const second = deferred();
    vi.mocked(fetchSkillStats)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const { result, rerender } = renderHook(
      ({ snapshot }) => usePlanStats(snapshot, snapshot, "rm"),
      { initialProps: { snapshot: plans } },
    );
    rerender({ snapshot: [{ ...plans[0], status: "已自动下发" }] });
    expect(fetchSkillStats).toHaveBeenCalledTimes(1);
    await act(async () => first.resolve({}));
    expect(fetchSkillStats).toHaveBeenCalledTimes(2);
    const latest = { a: { targetCustomerCount: 24, generatedTaskCount: 6 } };
    await act(async () => second.resolve(latest));
    expect(result.current).toMatchObject({
      sceneStats: latest,
      statsLoading: false,
      statsUpdating: false,
    });
  });
  it("手动刷新版本变化时绕过有效期，更新后保留缓存", async () => {
    const plans = [plan("a")];
    const { result, rerender } = renderHook(
      ({ version }) => usePlanStats(plans, plans, "rm", version),
      { initialProps: { version: 0 } },
    );
    await waitFor(() => expect(result.current.statsLoading).toBe(false));
    rerender({ version: 1 });
    await waitFor(() => expect(result.current.statsUpdating).toBe(false));
    expect(fetchSkillStats).toHaveBeenCalledTimes(2);
    rerender({ version: 1 });
    expect(fetchSkillStats).toHaveBeenCalledTimes(2);
  });
  it("统计请求15秒超时后释放加载状态，手动重试成功后旧响应不覆盖", async () => {
    vi.useFakeTimers();
    const plans = [plan("a")];
    const old = deferred();
    let signal!: AbortSignal;
    const latest = { a: { targetCustomerCount: 24, generatedTaskCount: 6 } };
    vi.mocked(fetchSkillStats)
      .mockImplementationOnce((_, input) => {
        signal = input!;
        return old.promise;
      })
      .mockResolvedValueOnce(latest);
    const { result, rerender } = renderHook(
      ({ version }) => usePlanStats(plans, plans, "rm", version),
      { initialProps: { version: 0 } },
    );
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(signal.aborted).toBe(true);
    expect(result.current).toMatchObject({
      sceneStats: null,
      statsLoading: false,
      statsUpdating: false,
      statsFailed: true,
    });
    await act(async () => rerender({ version: 1 }));
    expect(result.current.sceneStats).toEqual(latest);
    await act(async () =>
      old.resolve({ a: { targetCustomerCount: 1, generatedTaskCount: 1 } }),
    );
    expect(result.current.sceneStats).toEqual(latest);
  });

  it("后台统计超时保留旧数字并解除更新状态", async () => {
    vi.useFakeTimers();
    const plans = [plan("a")];
    const stats = { a: { targetCustomerCount: 12, generatedTaskCount: 3 } };
    vi.mocked(fetchSkillStats)
      .mockResolvedValueOnce(stats)
      .mockReturnValueOnce(new Promise(() => {}));
    const { result, rerender } = renderHook(
      ({ version }) => usePlanStats(plans, plans, "rm", version),
      { initialProps: { version: 0 } },
    );
    await act(async () => {});
    rerender({ version: 1 });
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(result.current).toMatchObject({
      sceneStats: stats,
      statsLoading: false,
      statsUpdating: false,
      statsFailed: true,
    });
  });
});
