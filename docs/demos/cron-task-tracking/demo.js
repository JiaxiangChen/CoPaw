(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? "未记录").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const icon = (name) =>
    `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const categories = {
    all: "全部事件",
    management: "管理操作",
    dispatch: "调度记录",
    execution: "执行结果",
    notification: "通知记录",
  };
  const entries = {
    batch: "批调度",
    ordinary: "普通调度",
    manual: "手动执行",
    retry: "失败补跑",
  };
  const mainId = "7f2b4190-6a38-4d19-9c62-047abf137e20";
  const tasks = [
    {
      key: "parent",
      id: mainId,
      name: "客户经营日报",
      role: "父任务",
      type: "Workflow",
      mode: "批调度",
      user: "演示用户 · demo-user-01",
      last: "成功",
      note: "已发送 · 未读",
    },
    {
      key: "child",
      id: "demo-child-001",
      name: "重点客户经营日报",
      role: "子任务",
      type: "Workflow",
      mode: "批调度",
      user: "演示用户 · demo-user-02",
      last: "成功",
      note: "已发送 · 未读",
    },
    {
      key: "ordinary",
      id: "demo-ordinary-001",
      name: "每日市场简报",
      role: "普通任务",
      type: "Agent",
      mode: "普通调度",
      user: "演示用户 · demo-user-03",
      last: "成功",
      note: "已发送 · 已读",
    },
    {
      key: "idle",
      id: "demo-idle-001",
      name: "新建经营提醒",
      role: "普通任务",
      type: "Agent",
      mode: "普通调度",
      user: "演示用户 · demo-user-04",
      last: "尚未执行",
      note: "暂无通知",
    },
  ];
  const event = (
    id,
    date,
    time,
    category,
    title,
    state,
    summary,
    entry,
    reason,
    ref,
    extra = {},
  ) => ({
    id,
    time: `${date}T${time}+08:00`,
    category,
    title,
    state,
    summary,
    entry,
    reason,
    ref,
    ...extra,
  });
  const today = (
    id,
    time,
    category,
    title,
    state,
    summary,
    entry,
    reason,
    ref,
    extra,
  ) =>
    event(
      id,
      "2026-10-28",
      time,
      category,
      title,
      state,
      summary,
      entry,
      reason,
      ref,
      extra,
    );
  const history = [
    event(
      "old-create",
      "2026-10-26",
      "08:30:00",
      "management",
      "创建任务",
      "success",
      "已保存任务定义，完成资源准备与调度同步。",
      null,
      "管理员创建",
      "op-demo-create",
      {
        actor: "演示管理员",
        source: "管理操作单",
        fields: [
          ["提交版本", "v1"],
          ["排程（提交值）", "0 9 * * 1-5"],
        ],
      },
    ),
    event(
      "old-run",
      "2026-10-27",
      "09:01:42",
      "execution",
      "执行失败",
      "error",
      "当次执行因配置缺失失败，未产生成功结果通知。",
      "ordinary",
      "按计划触发",
      "run-demo-1027",
      {
        source: "执行历史",
        fields: [
          ["主执行结果", "失败"],
          ["错误说明", "workflow secret value missing: auth_token"],
          ["耗时", "1 分 42 秒"],
        ],
      },
    ),
    event(
      "old-retry",
      "2026-10-27",
      "09:30:00",
      "dispatch",
      "受理失败补跑",
      "processing",
      "补跑已受理，沿用原计划周期并关联此前失败执行。",
      "retry",
      "当日失败补跑",
      "run-demo-retry-1027",
      {
        source: "补跑记录",
        fields: [
          ["前次执行", "run-demo-1027"],
          ["原计划时间", "2026-10-27 09:00:00"],
        ],
      },
    ),
    event(
      "old-retry-result",
      "2026-10-27",
      "09:32:00",
      "execution",
      "补跑执行完成",
      "success",
      "重新执行后整轮结果已确认成功。",
      "retry",
      "当日失败补跑",
      "run-demo-retry-1027",
      {
        source: "执行历史",
        fields: [
          ["主执行结果", "成功"],
          ["整轮结果", "成功"],
        ],
      },
    ),
    event(
      "old-notify",
      "2026-10-27",
      "09:33:00",
      "notification",
      "通知处理完成",
      "success",
      "补跑结果的通知标记为已发送，后续已标读。",
      "retry",
      "补跑执行结果通知",
      "run-demo-retry-1027",
      {
        source: "通知状态",
        fields: [
          ["原应通知时间", "2026-10-27 09:01:00"],
          ["记录的发送时间", "2026-10-27 09:33:00"],
          ["通知标记", "已发送"],
          ["已读状态", "已读"],
        ],
      },
    ),
  ];
  const records = [
    today(
      "modify",
      "07:55:00",
      "management",
      "修改任务",
      "success",
      "调整任务内容及通知延迟，操作记录显示已完成。",
      null,
      "管理员修改",
      "op-demo-update",
      {
        actor: "演示管理员",
        source: "管理操作单",
        fields: [
          ["提交版本", "v3"],
          ["通知延迟（提交值）", "1 分钟"],
          ["最近更新时间", "2026-10-28 07:55:03"],
        ],
        note: "按操作提交时间入轴；操作单不保留完整步骤流水，不推测中间过程。",
      },
    ),
    today(
      "mode",
      "08:02:00",
      "management",
      "切换为批调度",
      "success",
      "目标调度方式为开启批调度，已有记录显示同步完成。",
      null,
      "管理员切换调度方式",
      "op-demo-mode",
      {
        actor: "演示管理员",
        source: "管理操作单",
        fields: [
          ["目标模式", "开启批调度"],
          ["操作状态", "已完成"],
          ["最近更新时间", "2026-10-28 08:02:02"],
        ],
      },
    ),
    today(
      "manual",
      "08:10:00",
      "dispatch",
      "受理手动执行",
      "processing",
      "已登记一次手动执行，受理不代表执行成功。",
      "manual",
      "人工立即执行",
      "run-demo-manual",
      {
        actor: "演示管理员",
        source: "执行记录",
        fields: [
          ["执行类型", "manual"],
          ["受理状态", "已入队"],
        ],
      },
    ),
    today(
      "manual-start",
      "08:10:03",
      "execution",
      "开始执行",
      "processing",
      "SWE 开始处理本次手动执行。",
      "manual",
      "人工立即执行",
      "run-demo-manual",
      {
        source: "执行事件",
        fields: [
          ["主执行状态", "运行中"],
          ["使用定义版本", "v3"],
        ],
      },
    ),
    today(
      "manual-error",
      "08:11:12",
      "execution",
      "执行失败",
      "error",
      "Workflow 运行失败：缺少所需的用户配置。",
      "manual",
      "人工立即执行",
      "run-demo-manual",
      {
        source: "执行历史",
        fields: [
          ["错误说明", "workflow secret value missing: auth_token"],
          ["公开错误码", "cron_execution_failed"],
          ["主执行耗时", "1 分 9 秒"],
          ["Trace ID", "trace-demo-manual-001"],
        ],
        note: "执行失败与通知失败分别展示；本次未产生成功结果通知。",
      },
    ),
    today(
      "trigger",
      "08:40:00",
      "dispatch",
      "受理批调度触发",
      "success",
      "批调度入口已受理，本任务进入本次调度范围。",
      "batch",
      "按计划提前准备结果",
      "run-demo-scheduled",
      {
        source: "触发回执",
        fields: [
          ["逻辑计划时间", "2026-10-28 09:00:00"],
          ["实际触发时间（已记录）", "2026-10-28 08:40:00"],
          ["我方接收时间", "2026-10-28 08:40:00"],
          ["外部定时器 ID", "timer-demo-2608"],
        ],
        note: "这里展示我方保存的触发回执，不代表实时查询外部平台状态。",
      },
    ),
    today(
      "queue",
      "08:40:01",
      "dispatch",
      "进入待派发队列",
      "processing",
      "执行记录已登记，等待后台派发。",
      "batch",
      "批调度入口触发",
      "run-demo-scheduled",
      {
        source: "执行事件",
        fields: [
          ["执行状态", "queued"],
          ["计划时间", "2026-10-28 09:00:00"],
          ["排队原因", "未记录"],
        ],
        note: "没有原因记录时，不推断为容量不足、限流或其他故障。",
      },
    ),
    today(
      "send",
      "08:40:04",
      "dispatch",
      "派发至 SWE",
      "processing",
      "Scheduler 已发起本次执行派发。",
      "batch",
      "待派发执行被领取",
      "run-demo-scheduled",
      {
        source: "执行事件",
        fields: [
          ["执行状态", "dispatching"],
          ["使用定义版本", "v3"],
        ],
      },
    ),
    today(
      "accept",
      "08:40:06",
      "execution",
      "SWE 已受理",
      "processing",
      "SWE 已确认受理，等待实际开始执行。",
      "batch",
      "批调度入口触发",
      "run-demo-scheduled",
      { source: "执行事件", fields: [["执行状态", "accepted"]] },
    ),
    today(
      "start",
      "08:40:08",
      "execution",
      "开始执行",
      "processing",
      "开始执行 Workflow，使用本次执行保存的任务定义。",
      "batch",
      "批调度入口触发",
      "run-demo-scheduled",
      {
        source: "执行事件",
        fields: [
          ["执行状态", "running"],
          ["使用定义版本", "v3"],
          ["Trace ID", "trace-demo-scheduled-001"],
        ],
      },
    ),
    today(
      "result",
      "08:41:30",
      "execution",
      "主执行完成",
      "success",
      "主执行成功，异步结果尚未确认完成。",
      "batch",
      "批调度入口触发",
      "run-demo-scheduled",
      {
        source: "执行历史",
        fields: [
          ["主执行结果", "成功"],
          ["当时异步结果", "待确认"],
          ["输出摘要", "已生成经营分析摘要，等待异步结果完成。"],
        ],
        note: "主执行成功不等于整轮执行成功，以异步结果收敛记录为准。",
      },
    ),
    today(
      "settled",
      "08:42:15",
      "execution",
      "整轮结果已确认",
      "success",
      "异步结果已完成，本次执行整轮成功。",
      "batch",
      "异步结果收敛",
      "run-demo-scheduled",
      {
        source: "执行事件",
        fields: [
          ["主执行结果", "成功"],
          ["整轮结果", "成功"],
          [
            "输出摘要",
            "客户经营日报已生成，包含客户概况、经营建议及待跟进事项。",
          ],
          ["历史记录 ID", "history-demo-1028"],
        ],
        note: "只展示此任务自己的异步结果状态，不展开其他定时任务。",
      },
    ),
    today(
      "notification",
      "09:01:00",
      "notification",
      "通知处理完成",
      "success",
      "已标记发送，当前未读。通知发送与用户已读分开记录。",
      "batch",
      "执行结果通知",
      "run-demo-scheduled",
      {
        source: "通知状态",
        fields: [
          ["应通知时间", "2026-10-28 09:01:00"],
          ["记录的发送时间", "2026-10-28 09:01:00"],
          ["累计通知失败次数", "1 次"],
          ["已读状态", "未读"],
        ],
        note: "历史失败的具体发生时间未记录，仅展示累计次数，不在时间轴中补造失败节点。",
      },
    ),
  ];
  let task = tasks[0],
    category = "all",
    descending = false;
  let activeStart = "2026-10-28T00:00",
    activeEnd = "2026-10-28T23:59";
  let requestGeneration = 0;
  const opened = new Set();
  const labels = { success: "已完成", error: "失败", processing: "已记录" };
  const icons = {
    management: "settings",
    dispatch: "clock",
    execution: "play",
    notification: "bell",
  };

  function taskEvents() {
    if (!task) return [];
    if (task.key === "idle")
      return [
        event(
          "idle-create",
          "2026-10-28",
          "08:50:00",
          "management",
          "创建任务",
          "success",
          "任务已创建，尚未产生执行记录。",
          null,
          "管理员创建",
          "op-demo-idle",
          {
            source: "管理操作单",
            actor: "演示管理员",
            fields: [["操作结果", "已完成"]],
          },
        ),
      ];
    const all = [...history, ...records];
    if (task.key === "ordinary")
      return all
        .filter(
          (e) =>
            !["mode", "manual", "manual-start", "manual-error"].includes(e.id),
        )
        .map((e) => ({
          ...e,
          entry: e.entry === "batch" ? "ordinary" : e.entry,
          title: e.id === "trigger" ? "受理普通调度触发" : e.title,
          summary: e.summary.replace("批调度入口", "普通调度入口"),
          reason: e.reason.replace("批调度入口", "普通调度入口"),
        }));
    if (task.key === "child")
      return all
        .filter(
          (e) =>
            !["mode", "manual", "manual-start", "manual-error"].includes(e.id),
        )
        .map((e) =>
          e.id === "trigger"
            ? {
                ...e,
                title: "批调度将本任务入队",
                summary:
                  "本任务随已受理的批调度进入队列；这里只展示此任务的记录。",
              }
            : e,
        );
    return all;
  }
  function renderOverview() {
    $(
      "overview",
    ).innerHTML = `<div class="overview-top"><div><div class="task-title"><h2 data-testid="task-name">${esc(
      task.name,
    )}</h2><span class="tag" data-testid="task-role">${esc(
      task.role,
    )}</span><span class="tag neutral">${esc(
      task.type,
    )}</span><span class="tag success">已启用</span></div><div class="task-id"><span>${esc(
      task.id,
    )}</span><button class="copy-button" id="copy-id" title="复制任务 ID" aria-label="复制任务 ID">${icon(
      "copy",
    )}</button></div></div><div class="snapshot-label">当前属性快照<span>截至 2026-10-28 09:06 · 非历史属性</span></div></div>
      <dl class="meta-grid">${[
        ["所属用户", task.user],
        ["来源", "RMASSIST"],
        ["Agent", "default"],
        ["调度方式", task.mode],
        ["执行排程", "0 9 * * 1-5"],
        ["时区", "Asia/Shanghai"],
        ["当前定义版本", task.key === "idle" ? "v1" : "v3"],
        ["下次计划", "2026-10-29 09:00:00"],
      ]
        .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`)
        .join("")}</dl>
      <div class="task-state-row"><span>管理状态<strong><span class="state-dot"></span>就绪</strong></span><span>最近主执行<strong>${esc(
        task.last,
      )}</strong></span><span>整轮结果<strong>${
        task.key === "idle" ? "暂无记录" : "成功"
      }</strong></span><span>通知<strong>${esc(
        task.note,
      )}</strong></span></div>${
        task.key === "idle" ? '<p class="idle-note">尚未产生执行记录</p>' : ""
      }`;
    $("copy-id").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(task.id);
        $("query-status").textContent = "任务 ID 已复制";
      } catch {
        $("job-id").focus();
        $("job-id").select();
        $("query-status").textContent = "已选中任务 ID，可手动复制";
      }
    });
  }
  function timeRangeEvents() {
    const from = Date.parse(`${activeStart}:00+08:00`),
      to = Date.parse(`${activeEnd}:59+08:00`);
    return taskEvents().filter(
      (e) =>
        Date.parse(e.time) >= from &&
        Date.parse(e.time) <= to &&
        ($("entry").value === "all" || e.entry === $("entry").value),
    );
  }
  function renderTimeline() {
    if (!task) return;
    const inRange = timeRangeEvents();
    $("categories").innerHTML = Object.entries(categories)
      .map(
        ([key, label]) =>
          `<button type="button" data-category="${key}" aria-pressed="${
            category === key
          }">${label}<span class="count">${
            key === "all"
              ? inRange.length
              : inRange.filter((e) => e.category === key).length
          }</span></button>`,
      )
      .join("");
    const visible = inRange
      .filter((e) => category === "all" || e.category === category)
      .sort(
        (a, b) =>
          (Date.parse(a.time) - Date.parse(b.time) ||
            a.id.localeCompare(b.id)) * (descending ? -1 : 1),
      );
    $("result-count").textContent = `${visible.length} 条记录`;
    $("empty-range").hidden = visible.length !== 0;
    let day = "";
    $("event-list").innerHTML = visible
      .map((e) => {
        const date = e.time.slice(0, 10),
          group =
            date !== day
              ? `<div class="day-heading">${date.replaceAll(
                  "-",
                  " / ",
                )}<small>${
                  date === "2026-10-28" ? "今天" : "历史记录"
                }</small><span class="day-rule"></span></div>`
              : "";
        day = date;
        const isOpen = opened.has(e.id);
        const fields = [
          ["记录时间", e.time.slice(0, 19).replace("T", " ") + "（北京时间）"],
          ["数据来源", e.source || "已保存记录"],
          [e.category === "management" ? "操作 ID" : "执行 ID", e.ref],
          ...(e.fields || []),
        ];
        return `${group}<article class="timeline-event" data-id="${
          e.id
        }" data-category="${
          e.category
        }"><time class="event-time" datetime="${esc(e.time)}">${e.time.slice(
          11,
          19,
        )}</time><div class="event-marker"><span class="event-icon ${
          e.state
        }">${icon(
          e.state === "error"
            ? "alert"
            : e.category === "execution" && e.state === "success"
            ? "check"
            : icons[e.category],
        )}</span></div><div class="event-body"><div class="event-heading"><h3>${esc(
          e.title,
        )}</h3><span class="tag ${
          e.state === "processing" ? "neutral" : e.state
        }">${labels[e.state]}</span><span class="category-name">${
          categories[e.category]
        }</span><button type="button" class="details-toggle" data-detail="${
          e.id
        }" aria-expanded="${isOpen}" aria-controls="detail-${e.id}">${
          isOpen ? "收起详情" : "查看详情"
        }${icon("chevron")}</button></div><p class="event-summary">${esc(
          e.summary,
        )}</p><div class="event-metadata">${
          e.entry ? `<span>入口<strong>${entries[e.entry]}</strong></span>` : ""
        }<span>原因<strong>${esc(e.reason)}</strong></span>${
          e.actor
            ? `<span>操作人<strong>${esc(e.actor)}</strong></span>`
            : `<span>执行<strong><code>${esc(e.ref)}</code></strong></span>`
        }</div><div class="event-detail" id="detail-${e.id}" ${
          isOpen ? "" : "hidden"
        }><dl>${fields
          .map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`)
          .join("")}</dl>${
          e.note ? `<p class="detail-note">${esc(e.note)}</p>` : ""
        }</div></div></article>`;
      })
      .join("");
  }
  function setTask(next) {
    task = next;
    $("not-found").hidden = Boolean(task);
    $("overview").hidden = !task;
    $("timeline-section").hidden = !task;
    if (task) {
      $("scenario").value = task.key;
      renderOverview();
      renderTimeline();
    } else {
      $("overview").replaceChildren();
      $("event-list").replaceChildren();
    }
  }
  function setRange(key) {
    activeStart =
      key === "three"
        ? "2026-10-26T00:00"
        : key === "seven"
        ? "2026-10-22T00:00"
        : "2026-10-28T00:00";
    activeEnd = "2026-10-28T23:59";
    $("start").value = activeStart;
    $("end").value = activeEnd;
    $("range-error").hidden = true;
    document
      .querySelectorAll("[data-range]")
      .forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.range === key)),
      );
    renderTimeline();
  }
  $("query-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const generation = ++requestGeneration,
      id = $("job-id").value.trim();
    $("query-button").disabled = true;
    $("query-button").textContent = "查询中…";
    await new Promise((resolve) => setTimeout(resolve, 180));
    if (generation !== requestGeneration) return;
    const found = tasks.find((t) => t.id === id);
    opened.clear();
    setTask(found || null);
    $("query-button").disabled = false;
    $("query-button").textContent = "查询任务";
    $("query-status").textContent = found ? "已载入模拟记录" : "";
  });
  function showExample(key = "parent") {
    requestGeneration += 1;
    descending = false;
    $("sort").querySelector("span").textContent = "时间正序";
    category = "all";
    opened.clear();
    $("entry").value = "all";
    $("query-button").disabled = false;
    $("query-button").textContent = "查询任务";
    setTask(tasks.find((t) => t.key === key));
    $("job-id").value = task.id;
    setRange("today");
    $("query-status").textContent = "";
  }
  $("scenario").addEventListener("change", (e) => showExample(e.target.value));
  $("reset").addEventListener("click", () => showExample());
  $("back-example").addEventListener("click", () => showExample());
  $("show-seven").addEventListener("click", () => {
    category = "all";
    $("entry").value = "all";
    setRange("seven");
  });
  document
    .querySelectorAll("[data-range]")
    .forEach((b) =>
      b.addEventListener("click", () => setRange(b.dataset.range)),
    );
  $("apply-range").addEventListener("click", () => {
    const from = $("start").value,
      to = $("end").value;
    if (!from || !to || from > to) {
      $("range-error").textContent =
        "请选择有效时间范围，开始时间不能晚于结束时间。";
      $("range-error").hidden = false;
      return;
    }
    activeStart = from;
    activeEnd = to;
    $("range-error").hidden = true;
    document
      .querySelectorAll("[data-range]")
      .forEach((b) => b.setAttribute("aria-pressed", "false"));
    renderTimeline();
  });
  $("entry").addEventListener("change", renderTimeline);
  $("categories").addEventListener("click", (e) => {
    const button = e.target.closest("[data-category]");
    if (button) {
      category = button.dataset.category;
      renderTimeline();
    }
  });
  $("sort").addEventListener("click", () => {
    descending = !descending;
    $("sort").querySelector("span").textContent = descending
      ? "时间倒序"
      : "时间正序";
    renderTimeline();
  });
  $("refresh").addEventListener("click", () => {
    if (task) {
      renderOverview();
      renderTimeline();
      $("query-status").textContent = "演示记录已刷新（非实时数据）";
    }
  });
  $("event-list").addEventListener("click", (e) => {
    const button = e.target.closest("[data-detail]");
    if (!button) return;
    const id = button.dataset.detail;
    if (opened.has(id)) opened.delete(id);
    else opened.add(id);
    const open = opened.has(id);
    $("detail-" + id).hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    button.innerHTML = (open ? "收起详情" : "查看详情") + icon("chevron");
  });
  showExample();
})();
