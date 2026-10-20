async (page) => {
  const root = "http://127.0.0.1:5187";
  const id = "cron-editor-check";
  await page.addInitScript(() => {
    document.cookie = "userid=80280195; path=/";
    window.__cronEditorSaves = [];
  });
  await page.setViewportSize({ width: 1468, height: 1000 });
  await page.route(root + "/env-grey-control.js", (route) => route.fulfill({
    contentType: "text/javascript",
    body: "window.__env__={directAccessUserWhitelist:['80280195']};",
  }));
  await page.route(root + "/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const cron = new URL(page.url()).searchParams.get("scenario") === "multi"
      ? "0 9,10,11 * * *" : "0 10 * * *";
    const spec = {
      id, name: "时间回归测试", enabled: true, task_type: "text", text: "测试正文",
      schedule: { type: "cron", cron, timezone: "Asia/Shanghai" },
      dispatch: { type: "channel", channel: "console", mode: "final",
        target: { user_id: "80280195", session_id: "cron-test-session" } },
      runtime: { max_concurrency: 1, misfire_grace_seconds: 300, timeout_seconds: 7200 },
      meta: {}, tenant_id: "80280195", source_id: "RMASSIST", agent_id: "default",
    };
    const state = {
      management_owner: "scheduler", management_status: "ready", deleted: false,
      dispatch_state: "ordinary", definition_version: 2, management_version: 2,
      state_version: 2, state: { last_status: "idle" }, task: {},
    };
    let value = [];
    if (path.endsWith("/auth/status")) value = { enabled: false };
    else if (path === "/api/scheduler/cron/jobs") {
      value = { items: [{ ...spec, ...state }], next_cursor: null };
    } else if (path === "/api/scheduler/cron/jobs/" + id) {
      if (route.request().method() === "PUT") {
        await page.evaluate((data) => window.__cronEditorSaves.push(data), route.request().postDataJSON());
        value = { operation: { operation_id: "edit-check", status: "completed" } };
      } else value = { spec, ...state };
    } else if (path.includes("timezone")) value = { timezone: "Asia/Shanghai" };
    else if (path.includes("/skills")) value = { skills: [] };
    await route.fulfill({ json: value });
  });
  await page.goto(root + "/scheduler-cron-jobs?origin=Y&sapId=80280195&source=RMASSIST");
}
