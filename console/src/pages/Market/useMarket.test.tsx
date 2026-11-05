import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { marketApi, type MarketBrowseResponse } from "../../api/modules/market";
import { useMarket } from "./useMarket";

vi.mock("../../api/modules/market", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("../../api/modules/market")
  >();
  return {
    ...actual,
    marketApi: { ...actual.marketApi, browseMarket: vi.fn() },
  };
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function response(id = "one", count = 0): MarketBrowseResponse {
  return {
    resource_type: "skill",
    items: [
      {
        item_id: id,
        name: id,
        description: "desc",
        version: "1",
        creator_id: "u",
        creator_name: "U",
        category_id: null,
        bbk_ids: [],
        status: "active",
        created_at: null,
        updated_at: null,
        call_count: count,
        user_count: count,
      },
    ],
    total: 1,
    category_total: 1,
    branch_total: 1,
    categories: [],
    branches: [],
  };
}
afterEach(() => {
  vi.restoreAllMocks();
  vi.mocked(marketApi.browseMarket).mockReset();
});

describe("independent skill statistics", () => {
  it("does not present fallback zero counts as successful statistics", async () => {
    vi.mocked(marketApi.browseMarket)
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce({ ...response(), stats_available: false });
    const { result } = renderHook(() => useMarket("src"));
    await act(async () => {
      await result.current.refreshSkills();
    });
    await waitFor(() => expect(result.current.statsStatus).toBe("error"));
    expect(result.current.skills[0].item_id).toBe("one");
  });

  it("ignores responses from the previous source", async () => {
    const oldStats = deferred<MarketBrowseResponse>();
    vi.mocked(marketApi.browseMarket)
      .mockResolvedValueOnce(response())
      .mockReturnValueOnce(oldStats.promise);
    const { result, rerender } = renderHook(({ source }) => useMarket(source), {
      initialProps: { source: "first" },
    });
    await act(async () => {
      await result.current.refreshSkills();
    });
    rerender({ source: "second" });
    await act(async () => {
      oldStats.resolve(response("one", 99));
    });
    expect(result.current.skills[0].call_count).toBe(0);
  });

  it("does not request statistics for an empty list", async () => {
    vi.mocked(marketApi.browseMarket).mockResolvedValueOnce({
      ...response(),
      items: [],
    });
    const { result } = renderHook(() => useMarket("src"));
    await act(async () => {
      await result.current.refreshSkills();
    });
    expect(result.current.statsStatus).toBe("ready");
    expect(marketApi.browseMarket).toHaveBeenCalledTimes(1);
  });
  it("publishes cards before statistics resolve", async () => {
    const stats = deferred<MarketBrowseResponse>();
    vi.mocked(marketApi.browseMarket)
      .mockResolvedValueOnce(response())
      .mockReturnValueOnce(stats.promise);
    const { result } = renderHook(() => useMarket("src"));
    await act(async () => {
      await result.current.refreshSkills();
    });
    expect(result.current.skills[0].item_id).toBe("one");
    expect(result.current.loading).toBe(false);
    expect(result.current.statsStatus).toBe("loading");
    expect(marketApi.browseMarket).toHaveBeenNthCalledWith(
      1,
      "src",
      "skill",
      expect.objectContaining({ includeStats: false }),
    );
    await act(async () => {
      stats.resolve(response("one", 9));
    });
    await waitFor(() => expect(result.current.skills[0].call_count).toBe(9));
    expect(result.current.statsStatus).toBe("ready");
  });

  it("keeps cards visible when statistics fail", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.mocked(marketApi.browseMarket)
      .mockResolvedValueOnce(response())
      .mockRejectedValueOnce(new Error("stats failed"));
    const { result } = renderHook(() => useMarket("src"));
    await act(async () => {
      await result.current.refreshSkills();
    });
    await waitFor(() => expect(result.current.statsStatus).toBe("error"));
    expect(result.current.skills[0].item_id).toBe("one");
    expect(result.current.loading).toBe(false);
  });

  it("ignores old statistics after refreshing the list", async () => {
    const oldStats = deferred<MarketBrowseResponse>();
    vi.mocked(marketApi.browseMarket)
      .mockResolvedValueOnce(response())
      .mockReturnValueOnce(oldStats.promise)
      .mockResolvedValueOnce(response("two"))
      .mockResolvedValueOnce(response("two", 7));
    const { result } = renderHook(() => useMarket("src"));
    await act(async () => {
      await result.current.refreshSkills();
    });
    await act(async () => {
      await result.current.refreshSkills();
    });
    await waitFor(() => expect(result.current.skills[0].call_count).toBe(7));
    await act(async () => {
      oldStats.resolve(response("one", 99));
    });
    expect(result.current.skills[0].item_id).toBe("two");
    expect(result.current.skills[0].call_count).toBe(7);
  });
});
