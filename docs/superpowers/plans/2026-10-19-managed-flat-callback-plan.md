# 新版平台回调兼容及每次独立受理 Implementation Plan

> **For agentic workers:** 用户 AGENTS 要求主线程顺序执行；使用 writing-plans、executing-plans、TDD 和主线程自查，不启动子代理，不冒充独立评审。

**Goal:** 平台只发送已解码 Body、没有 Header、平台 ID、流水号和时间时，新版普通/批回调均可校验并独立受理。

**Architecture:** 保留新版入口及共库校验，在回调适配层从 Body 补全身份；缺平台 ID 时使用当前持久绑定，仍核对 operation_id 和归属。每次生成独立回执，用回执 ID 作为成员执行 trigger_key，取消回调和跨入口周期去重。缺时间时以接收时间作为物理触发时间，逻辑时间为接收时间加已保存的提前量，不反推 cron 或因推测迟到拒绝请求；有明确时间时保留原时间格式和排程校验。

**Tech Stack:** FastAPI、Python uuid、共库事务及现有队列、pytest/ASGITransport/SQLite。

**Spec:** 本轮用户确认：调度平台不能传 Header，只有 tenant_id/source_id/agent_id/job_id/task_type/scopeId/fromId/callback_protocol/operation_id；不需要去重，每次视为有效的新触发。该决定替代原新版回调稳定日志/计划期去重约定，不改变旧入口、CRUD 请求号幂等和同一次执行的内部重试保护。

## Global Constraints

- 不新增配置、表或字段，不连接外部平台或业务数据库，不实际触发任务。
- 保留任务归属、scope 冲突、协议标记、有效注册引用、定义版本、管理就绪、删除/停用/暂停批调度和有效调度方式检查。
- Header 存在时不无条件覆盖；Body 只补缺失身份，不从 Body 接受管理角色。当前平台回调沿用可信内网入口边界；数据库引用核验不是新增来源认证。
- 每个 HTTP 请求各自生成执行/批次，即使 Body、时间或外部流水相同；历史已入队执行仍使用原 ID，恢复不重新受理。
- 批提前量、散列 due_at、优先级和成员冻结保持；独立回调不复用其他普通/批回调的成员执行。
- 保留所有既有未提交修改；不提交、不推送、不更新行内包。
- GitNexus impact：callback_caller/accept_callback/ordinary_callback/batch_callback、binding_context、trigger_times、accept、_enqueue_members 均 LOW；索引未识别 accept 的动态调用，但源码确认其调用限新版回调入口和测试。旧回调与旧调度器不改。

### Task 1: 用真实新入口复现固定 Body 和不去重需求

**Files:** Create tests/unit/scheduler/test_managed_cron_flat_callbacks.py；Modify tests/unit/scheduler/test_managed_cron_callbacks_api.py、test_managed_cron_callbacks.py、test_managed_batch_members.py。

- [ ] ASGI 对 ordinary/batch 两种新入口，用真实管理服务生成已注册任务，不传 Header，不传 external_job_id/trigger_id/时间，同一固定 Body 连续及并发各请求两次。
- [ ] 断言均 202、各有不同 execution_id/batch_id、独立回执及成员，reused=false；接收时间故意不匹配 cron，仍有效。
- [ ] 正向保留 jobParam 编码封装、显式 Header；反向覆盖 Header/Body 冲突、scope 错误、未知任务、错误注册引用、缺有效绑定和停用/删除/暂停。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/unit/scheduler/test_managed_cron_flat_callbacks.py -q --tb=short`，预期先因缺身份和缺时间/绑定支持失败，再实现。

### Task 2: 最小实现与全链路保护

**Files:** Modify scheduler/src/scheduler/app/routers/managed_callbacks.py；scheduler/src/scheduler/app/services/cron/managed/callback_contract.py；scheduler/src/scheduler/app/services/cron/managed/scheduling.py。

- [ ] callback_caller 对平铺和编码 Body 同样补缺失身份，拒绝非法值，检查 Body scope；接口说明明确平台无 Header 的固定协议。
- [ ] binding_context 缺 external_job_id/jobId 时从 state 当前普通/批绑定取值，有显式值时仍匹配；注册操作、版本和关系校验不放宽。
- [ ] trigger_times 缺时间时返回 now+advance、now；不依赖 trigger_id，不套 cron 匹配和推测时效；显式时间行为保持。
- [ ] accept 每次 uuid4 回执，callback_key 留空兼容旧列，删除按日志/周期查询旧回执的分支。_enqueue_members 使用 receipt_id 而不是 logical.isoformat 作为 ExecutionRequest.trigger_key，避免第二层执行复用。
- [ ] 按新契约更新原去重测试及普通/批跨入口测试，新增同一执行内部恢复幂等不受影响的回归检查；修改 HTTP 全链路重复请求断言，保留首次终态和历史断言。

### Task 3: 文档、检查与交付

**Files:** Modify docs/deployment/cron_phase2_deployment.md；在 docs/superpowers/specs/2026-10-15-zhituo-scheduler-cron-phase2-boundaries.md 标明本轮覆盖约定；不篡改历史实测报告。

- [ ] 给出用户固定 Body 的 Postman 示例和“不传 Header / 每次请求新执行”说明，明确手工重放也会新增执行；新队列内部恢复仍按原执行 ID。
- [ ] 运行 Scheduler 单元回归及 tests/integrated/scheduler/test_managed_cron_http_chain.py；MySQL 集成用例更新断言但本轮不连接业务或本地 MySQL，不冒充 MySQL 实测。
- [ ] 定向 pre-commit、复杂度上限 15、git diff --check；检查无旧接口和数据库 DDL 改动；主线程逐行自查。
- [ ] 报告实际测试数、跳过/环境限制；不把本地替身测试当成预发布验收，不新增提交或包。
