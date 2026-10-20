import { describe, expect, it } from "vitest";
import {
  detailToJob,
  RequestIdentity,
  selectedChildren,
  scheduleLabel,
  executionNeedsPolling,
} from "./helpers";
import type { TFunction } from "i18next";
import type { ManagedChild, ManagedJobDetail } from "@/api/types/schedulerCron";

describe("新版任务页面协议转换", () => {
  it("主成功但异步未确认继续查进度，旧版本无该字段仍按原终态处理", () => {
    const value = {
      execution_id: "e",
      job_id: "j",
      status: "success",
      settled_status: null,
    };
    expect(executionNeedsPolling(value)).toBe(true);
    expect(executionNeedsPolling({ ...value, settled_status: "error" })).toBe(
      false,
    );
    expect(executionNeedsPolling({ ...value, settled_status: "success" })).toBe(
      false,
    );
    expect(executionNeedsPolling({ ...value, settled_status: undefined })).toBe(
      false,
    );
  });
  it("排程保留原页友好文字，但不根据旧批元数据打标签", () => {
    const t = ((key: string) => key) as TFunction;
    expect(scheduleLabel("0 9 * * *", t)).toBe("cronJobs.cronTypeDaily 09:00");
    expect(scheduleLabel("30 10 * * mon", t)).toContain("cronJobs.cronDayMon");
    expect(scheduleLabel("7 3 1 * *", t)).toBe("7 3 1 * *");
  });
  it("详情身份来自 spec，管理版本保留在顶层", () => {
    const detail = {
      spec: { id: "j", tenant_id: "u", source_id: "s", name: "任务" },
      agent_id: "a",
      definition_version: 2,
      management_version: 5,
      state: { last_status: "success" },
    } as ManagedJobDetail;
    expect(detailToJob(detail)).toMatchObject({
      id: "j",
      tenant_id: "u",
      agent_id: "a",
      definition_version: 2,
      management_version: 5,
      state: { last_status: "success" },
    });
  });
  it("结果未知时禁止换请求内容，相同内容沿用幂等身份，显式新轮才能重置", () => {
    const request = new RequestIdentity();
    const first = request.for({ targets: ["u"] });
    expect(request.for({ targets: ["u"] })).toBe(first);
    expect(() => request.for({ targets: [] })).toThrow("上次请求");
    request.reset();
    expect(request.for({ targets: [] })).not.toBe(first);
  });
  it("子任务批操作携带目标版本和 Agent，不使用裸 ID", () => {
    const child = {
      id: "j",
      tenant_id: "u",
      agent_id: "a",
      definition_version: 3,
    } as ManagedChild;
    expect(selectedChildren([child])).toEqual([
      {
        job_id: "j",
        tenant_id: "u",
        agent_id: "a",
        expected_definition_version: 3,
      },
    ]);
  });
});
