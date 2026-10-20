# 新版批调度标识 Implementation Plan

> **For agentic workers:** 按用户 AGENTS 在主线程执行 TDD 和自查，不启动子代理；不提交、不推送或更新行内包。

**Goal:** 外部平台批入口名称加入批调度标注；新版管理页及子任务排程前显示旧页同款标签。

**Architecture:** 仅在 ManagedPlatformClient._payload 为批入口计算 `[Scheduler][批调度]` 名称前缀，普通名称不变。前端使用现有 `Tag color="processing"`，以 batch_enabled/batch_paused 两态判断，不依据过期旧 meta 字段，不改变实际排程。

**Tech Stack:** Python/httpx/pytest、React/Ant Design/TypeScript、Vitest、现有本地浏览器测试。

**Spec:** 用户两项标注要求；分发子任务同样显示批调度标签。只影响新任务通道，不修改旧页面或调度协议、任务定义名称、定时器身份、回调和执行行为。已经存在的外部任务不自动扫描改名，后续注册/更新时按新规则同步。

## 边界与影响

- 外部平台 batch_enabled、batch_paused 均带标注，ordinary 不带；200 字符上限和原 name 不变；register/update 共用规则。
- 主列表执行时间列、子任务管理排程列覆盖父/子任务及开启/暂停状态；普通任务即使带旧批 meta 也不误标。
- GitNexus _payload 影响 LOW：register/update 两个直接调用、3 个受影响符号、1 个模块。createManagedColumns 和 ManagedChildrenModal 前端符号未入索引，UNKNOWN，不冒称 LOW；源码确认仅新页面消费。
- 保留既有未提交工作；无 SQL、表、配置或真实平台注册请求，无存量批量改名。

## Task 1: 回归测试

- [ ] 修改 tests/unit/scheduler/test_managed_cron_clients.py：真实客户端 + MockTransport 验证三态注册和更新 payload 的 jobDesc/回调路径，batch 前缀保留在 200 字符截断内，spec.name 不被修改。
- [ ] 新建 console/src/pages/Control/SchedulerCronJobs/columns.test.tsx：实际渲染新列，验证父/子任务批开启/暂停均显示 processing 标签并位于自己的排程文字前；普通及带旧批 meta 的普通任务不显示；分发子任务名称标签保留。
- [ ] 修改 ManagedChildrenModal.test.tsx 的现有用户/分页测试，增加批子任务排程格中标签和原 Cron 断言，覆盖 ordinary 不标。
- [ ] 先运行这些测试看到预期缺标注失败，再实现。

## Task 2: 最小实现

- [ ] 修改 platform.py 的 _payload 名称前缀计算，不改 spec 或平台注册参数其余字段。
- [ ] 在 helpers.ts 增加 isBatchDispatchMode；columns.tsx 的新 Cron render 接收任务记录，用现有 Tag 显示标识，文字保留 scheduleLabel。
- [ ] ManagedChildrenModal 排程格复用相同判定和标签，保留子任务原 Cron。

## Task 3: 验证

- [ ] Scheduler 单元及 HTTP 链路回归；前端新版页面目录单进程/20 秒超时回归；定向类型、格式、ESLint、Python pre-commit、复杂度及 diff 检查。
- [ ] 本地浏览器用测试数据截图普通、批父任务、批子任务和子任务弹窗；不调用真实平台注册或执行接口。
- [ ] 主线程逐行自查，报告实测范围和已有平台任务后续更新才同步名称的限制。
