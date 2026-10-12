/**
 * 智能财富工作台 —— 角色与页面权限测试
 */
import { describe, expect, it, vi } from "vitest";
import {
  canAccessPage,
  FALLBACK_ROLE,
  needsDistributeTargets,
  resolveRole,
  ROLE_PERMISSIONS,
} from "./permissions";

describe("WealthWorkbench permissions", () => {
  it("RB0108 理财主管岗（综合）沿用支行行长权限", () => {
    const role = resolveRole("RB0108");
    expect(role).toBe("president");
    expect(canAccessPage(role, "board")).toBe(true);
    expect(canAccessPage(role, "create")).toBe(true);
    expect(canAccessPage(role, "tasks")).toBe(false);
    expect(needsDistributeTargets(role)).toBe(true);
  });

  it("矩阵与原型一致：三角色可看板/创建，仅客户经理有任务页", () => {
    for (const role of ["rm", "president", "middle"] as const) {
      expect(canAccessPage(role, "board")).toBe(true);
      expect(canAccessPage(role, "create")).toBe(true);
    }
    expect(canAccessPage("rm", "tasks")).toBe(true);
    expect(canAccessPage("president", "tasks")).toBe(false);
    expect(canAccessPage("middle", "tasks")).toBe(false);
  });

  it("unknown 伪角色无任何页面权限", () => {
    for (const page of ["board", "create", "tasks"] as const) {
      expect(canAccessPage("unknown", page)).toBe(false);
    }
  });

  it("矩阵覆盖全部角色", () => {
    expect(Object.keys(ROLE_PERMISSIONS).sort()).toEqual([
      "middle",
      "president",
      "rm",
      "unknown",
    ]);
  });

  it("resolveRole 命中映射表：RB0101 客户经理 / RB1101、RB0306 行长 / RB0301、RB0305 中台", () => {
    expect(resolveRole("RB0101")).toBe("rm");
    expect(resolveRole("RB1101")).toBe("president");
    expect(resolveRole("RB0306")).toBe("president");
    expect(resolveRole("RB0301")).toBe("middle");
    expect(resolveRole("RB0305")).toBe("middle");
  });

  it("resolveRole 不再将旧编码 RB0304 识别为分行中台", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(resolveRole("RB0304")).toBe(FALLBACK_ROLE);
    expect(warn).toHaveBeenCalledOnce();
    warn.mockRestore();
  });

  it("仅行长/中台需要选择分发目标，客户经理发给自己", () => {
    expect(needsDistributeTargets("rm")).toBe(false);
    expect(needsDistributeTargets("president")).toBe(true);
    expect(needsDistributeTargets("middle")).toBe(true);
  });

  it("resolveRole 缺失或未知 positionId 回退 unknown（deny by default）", () => {
    // 未识别身份无任何页面权限，入口整页拦截
    expect(FALLBACK_ROLE).toBe("unknown");
    expect(canAccessPage(FALLBACK_ROLE, "board")).toBe(false);
    expect(canAccessPage(FALLBACK_ROLE, "create")).toBe(false);
    expect(canAccessPage(FALLBACK_ROLE, "tasks")).toBe(false);
    expect(resolveRole(null)).toBe(FALLBACK_ROLE);
    expect(resolveRole(undefined)).toBe(FALLBACK_ROLE);
    expect(resolveRole("")).toBe(FALLBACK_ROLE);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(resolveRole("UNKNOWN_POS")).toBe(FALLBACK_ROLE);
    expect(warn).toHaveBeenCalledOnce();
    warn.mockRestore();
  });
});
