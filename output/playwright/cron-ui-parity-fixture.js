async (page) => {
  // 仅本地浏览器验收：所有业务接口在 HTTP 边界替身，不接业务库或调度平台。
  const root = "http://127.0.0.1:5179";
  await page.addInitScript(() => {
    window.__env__ = { baseUrl: "http://127.0.0.1:5179" };
    sessionStorage.setItem("swe-iframe-context", JSON.stringify({ state: { userId: "preview-manager", source: "preview-bank", manager: true, isSuperManager: false, initialized: true }, version: 0 }));
  });
  const job = {
    id: "preview-parent", name: "客户经营日报", enabled: true,
    tenant_id: "preview-manager", source_id: "preview-bank", agent_id: "default",
    definition_version: 3, management_version: 1, management_status: "ready",
    management_owner: "scheduler", dispatch_state: "batch_enabled", deleted: false,
    schedule: { type: "cron", cron: "0 9 * * *", timezone: "Asia/Shanghai" },
    task_type: "text", text: "浏览器验收替身",
    dispatch: { type: "channel", channel: "console", target: { user_id: "preview-manager", session_id: "preview-session" } },
    state: { last_status: "success" },
  };
  const children = Array.from({ length: 12 }, (_, index) => ({
    ...job, id: `child-${index}`, job_id: `child-${index}`, name: `经营日报-${index + 1}`,
    tenant_id: index === 11 ? "NEEDLE-UID" : `user-${index + 1}`,
    tenant_name: index < 2 ? "张三" : `用户${index + 1}`,
    bbk_id: index < 2 ? "755" : "110", offset_minutes: index * 5,
    state: { last_status: index === 0 ? "error" : "success" },
  }));
  const calls = [];
  await page.route(root + "/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    calls.push({ path, method: route.request().method() });
    let body = [];
    if (path === "/api/auth/status") body = { enabled: false };
    else if (path.endsWith("/broadcast/children")) body = url.searchParams.has("cursor")
      ? { items: children.slice(6), next_cursor: null }
      : { items: children.slice(0, 6), next_cursor: "second" };
    else if (path.endsWith("/operations/pending")) body = { operation: null };
    else if (path.endsWith("/run")) body = { execution: { execution_id: "preview-exec", job_id: job.id, status: "queued" }, reused: false };
    else if (path.includes("/executions/")) body = { execution_id: "preview-exec", job_id: job.id, status: "success", settled_status: "success" };
    else if (path === "/api/scheduler/cron/jobs") body = { items: [job], next_cursor: null };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.setViewportSize({ width: 1600, height: 1100 });
  await page.goto(root + "/scheduler-cron-jobs");
  await page.evaluate(async () => {
    const module = await import("/src/stores/iframeStore.ts");
    module.useIframeStore.getState().setContext({ userId: "preview-manager", source: "preview-bank", manager: true });
  });
  await page.getByText(job.name, { exact: true }).waitFor();
  await page.getByRole("button", { name: `更多操作 ${job.name}` }).click();
  await page.getByText("子任务管理", { exact: true }).click();
  await page.getByText("张三 (2 个 UID)", { exact: true }).waitFor();
  await page.waitForFunction(() => [...document.querySelectorAll('.swe-spin-container')].every((node) => getComputedStyle(node, '::after').opacity === '0'));
  await page.screenshot({ path: "D:/Projects/CoPaw/output/playwright/cron-ui-children.png", fullPage: true, animations: "disabled" });
  await page.getByLabel("搜索子任务用户").fill(" needle-uid ");
  await page.getByRole("button", { name: /搜\s*索/ }).click();
  await page.getByText("NEEDLE-UID", { exact: true }).waitFor();
  if (await page.getByText("经营日报-1", { exact: true }).count()) throw new Error("搜索未过滤完整列表");
  await page.getByRole("combobox", { name: "筛选子任务机构" }).click();
  await page.locator(".swe-select-item-option").filter({ hasText: "755" }).click();
  if (await page.getByText("NEEDLE-UID", { exact: true }).count()) throw new Error("组合筛选不生效");
  await page.getByLabel("搜索子任务用户").fill("张三");
  await page.getByRole("button", { name: /搜\s*索/ }).click();
  await page.getByText("经营日报-2", { exact: true }).waitFor();
  await page.screenshot({ path: "D:/Projects/CoPaw/output/playwright/cron-ui-filtered.png", fullPage: true, animations: "disabled" });
  await page.getByRole("dialog").locator('.swe-table-body').evaluate((node) => { node.scrollLeft = node.scrollWidth; });
  await page.screenshot({ path: "D:/Projects/CoPaw/output/playwright/cron-ui-runtime-columns.png", fullPage: true, animations: "disabled" });
  await page.getByRole("dialog").getByRole("img", { name: "close", exact: true }).click();
  await page.getByRole("button", { name: "立即执行", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "立即执行任务" });
  await confirmation.waitFor();
  await confirmation.evaluate((node) => Promise.all(node.getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => {}))));
  await page.screenshot({ path: "D:/Projects/CoPaw/output/playwright/cron-ui-run-confirm.png", fullPage: true, animations: "disabled" });
  await confirmation.getByRole("button", { name: /取\s*消/ }).click();
  await confirmation.waitFor({ state: "hidden" });
  const cancelledCalls = calls.filter((call) => call.path.endsWith("/run")).length;
  if (cancelledCalls !== 0) throw new Error("取消仍发送执行请求");
  await page.getByRole("button", { name: "立即执行", exact: true }).click();
  const runResponse = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith("/run"));
  await page.getByRole("dialog", { name: "立即执行任务" }).getByRole("button", { name: "立即执行", exact: true }).click();
  await runResponse;
  const confirmedCalls = calls.filter((call) => call.path.endsWith("/run")).length;
  if (confirmedCalls !== 1) throw new Error("确认未恰好执行一次");
  return { cancelledCalls, confirmedCalls, childPageRequests: calls.filter((call) => call.path.endsWith("/broadcast/children")).length, note: "真实浏览器、HTTP Mock 边界" };
}
