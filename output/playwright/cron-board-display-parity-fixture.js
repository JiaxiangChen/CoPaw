async (page) => {
  const root = "http://127.0.0.1:5190";
  await page.addInitScript(() => {
    sessionStorage.setItem("swe-iframe-context", JSON.stringify({ state: {
      userId: "preview-manager", source: "preview-bank", manager: true,
      isSuperManager: false, initialized: true,
    }, version: 0 }));
  });
  const now = "2026-10-19T21:00:00+08:00";
  const batch = {
    batch_id: "preview-batch", job_id: "report-daily", job_name: "每日客户经营报告",
    tenant_id: "preview-manager", source_id: "preview-bank", agent_id: "default",
    member_count: 20, done_count: 20, failed_count: 2, success_count: 17, skipped_count: 1,
    active_count: 0, waiting_count: 0, status: "failed", completion_rate: 1,
    logical_fire_at: now, physical_fire_at: now, created_at: now,
    provider_id: "shared-provider", model_id: "shared-model", lock_owner: null,
  };
  const member = {
    member_id: "member-one", execution_id: "execution-one", tenant_id: "preview-user",
    source_id: "preview-bank", agent_id: "default", job_id: "report-child", job_name: "客户经营报告",
    business_attempt: 2, member_role: "child", member_order: 0,
    status: "error", settled_status: "error", error_code: "cron_execution_failed",
    error_message: "子任务执行失败：报告生成失败，请核对用户信息", settled_error_code: null,
    provider_id: "shared-provider", model_id: "shared-model", definition_version: 3,
    current_definition_version: 3, dispatch_attempt: 1, actor_id: "preview-manager",
    management_status: "ready", dispatch_state: "batch_enabled", bbk_id: "755",
    not_before_at: now, accepted_at: now, started_at: now, finished_at: now, settled_at: now,
    dispatch_priority: { basis: "user", user_rank: 1, branch_rank: 2, branch_id: "755" }, viewer_heat_score: 5,
  };
  const candidate = { ...member, member_version: 2, execution_version: 8,
    failure_type: "subtask_failed", transport_only: false, execution_definition_version: 3 };
  const capacity = { id: 1, worker_id: "shared-worker", source_id: "preview-bank",
    provider_id: "shared-provider", model_id: "shared-model", strategy_id: "baseline",
    previous_workers: 4, effective_workers: 3, baseline_workers: 4, min_workers: 1, max_workers: 8,
    pending_count: 12, claimed_count: 1, running_count: 3, success_count: 20, failure_count: 2,
    error_rate: 0.09, matched_rule: { threshold: 0.08, target: 3 }, avg_latency_ms: 2000,
    decision_reason: "错误率升高，降低并发", created_at: now };
  const workers = { source_id: "preview-bank", policies: [], current_capacity: [capacity],
    capacity_events: [capacity], capacity_events_next_cursor: null };
  await page.route(root + "/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body = [];
    if (path === "/api/auth/status") body = { enabled: false };
    else if (path.endsWith("/workers")) body = workers;
    else if (path.endsWith("/failures")) body = { counts: [{ failure_type: "subtask_failed", label: "子任务失败", count: 1 }], items: [candidate], matched_count: 1, has_more: false };
    else if (path === "/api/monitor/cron/managed-dispatch/batches") body = {
      items: [batch, { ...batch, batch_id: "paused-batch", job_name: "入口暂停但队列继续执行",
        status: "running", done_count: 10, success_count: 8, failed_count: 1,
        active_count: 2, waiting_count: 8, dispatch_paused: true, completion_rate: 0.5 }],
      total: 2, page: 1, page_size: 4,
      stats: { total_batches: 2, total_members: 40, done_members: 30, failed_members: 3, running_batches: 1 },
    };
    else if (path.startsWith("/api/monitor/cron/managed-dispatch/batches/")) body = {
      batch, members: [member], member_total: 20, events: [], event_total: 0,
    };
    await route.fulfill({ json: body });
  });
  await page.setViewportSize({ width: 1800, height: 1500 });
  await page.goto(root + "/monitor/cron-batch-dispatch-new");
  await page.evaluate(async () => {
    const module = await import("/src/stores/iframeStore.ts");
    module.useIframeStore.getState().setContext({ userId: "preview-manager", source: "preview-bank", manager: true });
  });
}
