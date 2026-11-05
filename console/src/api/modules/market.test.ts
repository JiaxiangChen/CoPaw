import { describe, expect, it, vi } from "vitest";

const requestMock = vi.fn();

vi.mock("../request", () => ({
  request: requestMock,
}));

describe("marketApi.browseMarket", () => {
  it("can request metadata without waiting for statistics", async () => {
    window.__env__ = { systemCode: "", systemSect: "" };
    const { marketApi } = await import("./market");
    await marketApi.browseMarket("source-a", "skill", { includeStats: false });
    expect(requestMock).toHaveBeenLastCalledWith(
      "/market/browse?resource_type=skill&include_stats=false",
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
  });
  it("sends the uncategorized filter without a fake category id", async () => {
    window.__env__ = {
      systemCode: "",
      systemSect: "",
    };
    requestMock.mockResolvedValueOnce({
      resource_type: "skill",
      items: [],
      total: 0,
      category_total: 0,
      branch_total: 0,
      categories: [],
      branches: [],
    });
    const { marketApi } = await import("./market");

    await marketApi.browseMarket("source-a", "skill", {
      categoryId: null,
      uncategorized: true,
    });

    expect(requestMock).toHaveBeenCalledWith(
      "/market/browse?resource_type=skill&uncategorized=true",
      expect.objectContaining({
        headers: expect.any(Headers),
      }),
    );
  });

  it("sends the orphaned filter", async () => {
    window.__env__ = {
      systemCode: "",
      systemSect: "",
    };
    requestMock.mockResolvedValueOnce({
      resource_type: "skill",
      items: [],
      total: 0,
      category_total: 0,
      branch_total: 0,
      categories: [],
      branches: [],
    });
    const { marketApi } = await import("./market");

    await marketApi.browseMarket("source-a", "skill", {
      categoryId: null,
      orphaned: true,
    });

    expect(requestMock).toHaveBeenCalledWith(
      "/market/browse?resource_type=skill&orphaned=true",
      expect.objectContaining({
        headers: expect.any(Headers),
      }),
    );
  });
});
