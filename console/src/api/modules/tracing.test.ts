import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
}));

vi.mock("../request", () => ({
  request: mocks.request,
}));

import { displaySkillName, tracingApi, type SkillUsage } from "./tracing";

const baseSkill = (overrides: Partial<SkillUsage> = {}): SkillUsage => ({
  skill_name: "weather",
  count: 1,
  avg_duration_ms: 0,
  ...overrides,
});

describe("displaySkillName", () => {
  it("prefers cn_name when present", () => {
    expect(
      displaySkillName(baseSkill({ cn_name: "天气查询" })),
    ).toBe("天气查询");
  });

  it("falls back to skill_name when cn_name is missing", () => {
    expect(displaySkillName(baseSkill())).toBe("weather");
  });

  it("falls back to skill_name when cn_name is empty string", () => {
    expect(displaySkillName(baseSkill({ cn_name: "" }))).toBe("weather");
  });

  it("falls back to skill_name when cn_name is whitespace only", () => {
    expect(displaySkillName(baseSkill({ cn_name: "   " }))).toBe("weather");
  });
});

describe("tracingApi.getOverview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("requests lightweight overview data when detail is summary", async () => {
    mocks.request.mockResolvedValue({});

    await tracingApi.getOverview("2026-06-01", "2026-06-30", "100", {
      detail: "summary",
      timeRange: "month",
    });

    expect(mocks.request).toHaveBeenCalledWith(
      "/monitor/tracing/overview?start_date=2026-06-01&end_date=2026-06-30&bbk_ids=100&detail=summary&time_range=month",
    );
  });
});

describe("tracingApi", () => {
  it("does not expose the removed depth summary request", () => {
    expect(tracingApi).not.toHaveProperty("getDepthSummary");
  });

  it("does not expose the removed growth stats request", () => {
    expect(tracingApi).not.toHaveProperty("getGrowthStats");
  });
});

it("loads target-user archives through the analytics route with encoded parameters", async () => {
  mocks.request.mockResolvedValue({ messages: [] });
  await tracingApi.getUserChatHistory("target user", "chat/id", "cursor+1", 20);
  expect(mocks.request).toHaveBeenLastCalledWith("/tracing/chats/chat%2Fid/history?user_id=target+user&limit=20&before=cursor%2B1");
});
