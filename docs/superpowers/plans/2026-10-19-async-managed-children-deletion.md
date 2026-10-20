# 新版子任务异步删除 Implementation Plan

> **For agentic workers:** 按用户 AGENTS 约定，使用 executing-plans 在主线程顺序执行 TDD、自查；不启动子代理，不提交、推送或修改已有暂存状态。行内包已经在本次改造前单独完成，不覆盖。

**Goal:** 选中删除和删除全部均持久受理后返回，由 Scheduler 后台处理，页面关闭后继续，重新打开可恢复查看进度。

**Architecture:** 复用 swe_cron_registration_journal 的 batch_delete 操作、固定 plans、租约和逐项删除。新增异步协调及只读进度视图，后台独立工作域恢复 pending/running；业务失败保留逐项结果，整体意外失败须人工重试。删除全部提交确认时读取的完整版本清单，不扩大到确认后新增的任务。

**Tech Stack:** FastAPI、共库操作日志、asyncio worker、React/Ant Design、pytest/SQLite、Vitest。

**Spec:** 用户要求先打当前行内包，然后把批量和全部删除改成异步。本计划覆盖旧设计中“同步删除/前端逐片等待/关闭停止下一片”的约定；旧 SWE 接口和普通单任务删除、批量执行不改变。

## Global Constraints

- 继续使用可信 Header 的完整源身份及真实操作人；逐项校验 parent 关系与版本，不能批量操作任意用户任务。
- 不新增表/配置，不执行 DDL。选中删除最多 1000 项；删除全部以确认时完整快照一次受理，不以筛选结果替代全量。
- 同 request_id 固定模式、清单及版本；网络未知重提复用原操作，GET 绝不发起删除或平台调用。
- 关闭页面不取消已受理后台操作；进度 GET 支持当前/指定操作，跨实例恢复共用租约围栏。
- 已开始执行继续；待清理与失败分开，失败清理沿用单任务人工续办，不自动重试业务失败。
- GitNexus：既有 BatchDeleteService.delete 2 个影响符号、1 个直接关联、0 个流程 LOW；ManagedRecoveryWorker LOW。新增前端符号未入索引 UNKNOWN，以实际调用方和测试补充，不冒称低风险。

### Task 1: 后台受理、只读进度与恢复

Files: services/cron/managed/async_batch_delete.py、batch_delete.py、recovery.py；routers/managed_cron_controls.py；tests/unit/scheduler/test_managed_cron_batch_delete_api.py、test_managed_cron_async_delete.py。

- [ ] 测试先证实 POST 必须返回 202/operation_id 且删除数量为0；GET 不能产生副作用；worker 执行后数量为2；重复提交不重复清理；非法身份/版本及原清单变更被拒绝。
- [ ] 分离 submit/get/current/process；process 重建原调用身份并复用稳定逐项操作ID。为全部快照支持大于1000项，但选中删除保持上限。工作域与分发/执行独立，不因关闭HTTP请求丢失工作。
- [ ] 覆盖中断租约恢复、两个worker竞争、意外失败后手动续办、待清理状态更新；无后台业务失败自动重试。
- [ ] 验证 `PYTHONPATH=src;scheduler/src .venv/Scripts/python.exe -m pytest tests/unit/scheduler/test_managed_cron_batch_delete.py tests/unit/scheduler/test_managed_cron_batch_delete_api.py tests/unit/scheduler/test_managed_cron_async_delete.py -q`。

### Task 2: 前端一次受理、轮询与重新进入

Files: console/src/api/{modules,types}/schedulerCron.ts；ManagedChildrenModal.tsx、childrenDeletion.ts 及对应测试。

- [ ] RED：1001项全部删除只有一次POST，网络失败重试原ID和版本；受理后不等待删除完成；重新进入查询当前进度，关闭停止轮询而不取消服务器任务。
- [ ] API 增加全部快照受理/当前及指定进度/原操作重试；页面沿用现有样式与结果表，显示后台排队、处理中、失败/待清理，不把受理当成功完成。
- [ ] 使用原 useManagedPanel 轮询生命周期，刷新/重新打开可恢复进度；互斥操作/错误重试和结束操作按钮与持久状态一致，迟到响应不更新卸载页面。
- [ ] 验证 `pnpm --dir console exec vitest run src/pages/Control/SchedulerCronJobs --maxWorkers=1 --testTimeout=20000`、typecheck、定向格式/lint。

### Task 3: 交付验收

- [ ] 执行 Scheduler 单元和 HTTP 链路、SWE结果回归、Python pre-commit/复杂度15、git diff --check。
- [ ] 本地浏览器HTTP替身核对异步进度、重新打开及按钮样式，明确不是业务库或预发布联调。
- [ ] 同步部署/对接文档及术语，说明先部署 Scheduler 再部署 Console；主线程自查，不宣称独立评审，不自动提交或更新改造前行内包。
