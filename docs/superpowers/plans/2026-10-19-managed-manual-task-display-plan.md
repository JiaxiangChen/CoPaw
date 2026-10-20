# 新版手动任务结果展示与未读 Implementation Plan

> **For agentic workers:** 按用户 AGENTS 主线程顺序执行 TDD 和自查，不启动子代理；不提交、不推送或打包，不改动已有暂存状态。

**Goal:** Scheduler 新任务手动“立即执行”成功后同样展示最新运行时间、累计未读，已有运行记录不再误提示未启动。

**Architecture:** SWE 成功提交将手动结果纳入已有展示字段和未读计数，历史保留 is_manual=1 但不默认已读；不加表或字段。前端新任务优先用 state.last_run_at 展示最新运行，必要时兼容旧手动记录，提示判断区分“运行过”与“有自动成功结果”；旧任务展示和执行写入不变。

**Tech Stack:** SWE ExecutionWriter、共库状态/历史、Scheduler 原 mark_read、React、Vitest、pytest 与本地浏览器。

**Spec:** 用户确认截图来自手动立即执行，并要求手动同样解决最新时间、未读次数和误提示三项问题。打开会话后仍按现有成功标读规则清除红点。历史已读记录不自动重置；手动累计未读但本次手动不直接触发自动未读暂停，通知和调度规则不改。

## 边界和影响

- 仅 Scheduler 管理的新任务；不改旧 SWE CronManager、旧任务 API 或已读水位，保留 is_manual 的执行属性。
- has_scheduled_result/latest_scheduled_preview/last_scheduled_run_at 保留兼容字段名，新版含手动和自动成功结果；未读和历史写入在原结果事务内一次提交，不因结果重试重复计数。
- 成功结果按 started_order 选择最新展示，晚完成的旧执行不覆盖更新结果；删除后不恢复任务状态。
- 新任务 state 时间是 UTC，前端对无时区的数据库时间按 UTC 解析，旧任务原格式不变；运行中和失败不伪装已完成。
- 不把本地 SQLite/HTTP 替身测试当成业务库或预发布验收；不回填或改写历史已读状态。
- GitNexus _result_state、_success_values、history_values、ChatPageContent、ChatTaskList、TasksContent 均 LOW；动态 React 引用仍用实际引用和测试核验，不据此宣称全量回归。

## Task 1: 先补回归测试

- [ ] tests/unit/app/test_managed_cron_execution.py：手动成功包含展示时间/预览、未读1、history is_read=0，重复同执行不累加；第二次独立手动结果累加，Scheduler 标读清水位和历史；开启自动暂停阈值1时手动不直接暂停。
- [ ] console/src/pages/Chat/taskJobs.test.ts：新任务有 last_run_at 即视为运行过，展示该时间而不是旧自动时间；时区及状态正确；legacy 不变。
- [ ] ChatTaskList、ExpandablePanel 实际渲染手动结果时间/次数；Chat/index.test.tsx 核对有运行时间时不提示未启动，无记录仍提示。
- [ ] 定向运行，确认因旧过滤/判断失败后再修改生产代码。

## Task 2: 最小实现

- [ ] managed_results._result_state 成功结果不再排除 manual，最新成功查询也包含手动；_success_values 参数明确为 allow_auto_pause，手动传 false，仅取消其直接自动暂停副作用。
- [ ] managed_history.history_values 新结果不预设已读，保留手动标记；Scheduler 原 mark_read 已覆盖全部运行关联历史，无需新接口。
- [ ] taskJobs 提供运行过判断和最新运行展示辅助函数，只有新版使用 state.last_run_at 和 UTC 归一化；两个侧栏组件共用，旧模式保持。
- [ ] Chat 提示按运行过判断，依赖真实状态时间更新；提示去重使用完整来源任务身份，不用裸ID。

## Task 3: 验证

- [ ] SWE 执行/结果、Scheduler 单元和 HTTP 链路；Chat 工具、侧栏、已读与页面测试；类型、格式、ESLint、定向 pre-commit 和复杂度15。
- [ ] 本地浏览器以测试数据检查时间、红点次数和有记录不误提示；不使用真实用户工作区或后台接口。
- [ ] 文档说明新手动结果未读、历史不重置及部署 SWE+Console；原 Scheduler 接口无变化。主线程自查，不创建提交或行内包。
