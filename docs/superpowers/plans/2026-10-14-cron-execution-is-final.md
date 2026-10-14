# Cron execution is_final Implementation Plan

**Goal:** 普通任务记录默认 is_final=1；批调度新结果事务性替换最新一条旧记录，覆盖自动和手动重试。

**Architecture:** Scheduler 是批调度反馈事务的所有者，锁定 intent 后重新校验当前轮次，复用同轮次记录，仅更新最新旧记录，再插入新记录。普通 Monitor 写入使用数据库默认值。上报协议、统计及查询不变。

**Tech Stack:** Python、FastAPI、aiomysql/MySQL、pytest。

**Spec:** `docs/superpowers/specs/2026-10-14-cron-execution-is-final-design.md`

## Global Constraints

- 历史全部为 1，不清洗。批调度每次最多将最新旧记录改为 0。
- 字段 `TINYINT(1) NOT NULL DEFAULT 1`，不允许客户端决定标记。
- 已确认范围不包含统计、Console、异步结算改动或生产 DDL 执行。
- 按 AGENTS.md 主线程顺序实施和自查，不派发子代理、不宣称独立评审。没有提交或推送授权。
- worktree 从更新后的 v1.0.0 `e327997a6` 创建，后按用户要求同步到 `4430b9239`，保留原工作区全部修改。
- 使用项目虚拟环境 `D:/Projects/CoPaw/.venv/Scripts/python.exe`；PYTHONPATH 指向本 worktree 的 `scheduler/src;monitor/src;.`。

## Task 1: 字段与原子写入测试

**Files:** 新建 `tests/unit/scheduler/test_cron_execution_is_final.py`。

- [x] 增加真实 SQLite 行数据的回归测试：默认 1、首次插入、两种重试、历史多 1 只替换最新、重复上报、错身份/轮次、插入失败回滚。SQLite 仅证明数据变更与回滚，不证明 MySQL 行锁。
- [x] 运行 `python -m pytest tests/unit/scheduler/test_cron_execution_is_final.py -q`，观察因字段和事务入口不存在而失败。

## Task 2: 手工加列与批调度落库

**Files:** `scheduler/src/scheduler/app/services/cron/execution_sync_service.py`、`scheduler/src/scheduler/app/routers/cron.py`、`scripts/sql/cron_execution_is_final.sql`。

- [x] 保留原 schema 声明，提供单次运行的手工加列 SQL，不 UPDATE 历史。
- [x] 批调度入口用同连接显式 begin、intent FOR UPDATE、校验、执行记录锁定读、单条 UPDATE、INSERT、commit；异常/取消 rollback。
- [x] 复用现有 INSERT，普通路径保持默认 1；Router 事务外初步校验仍保留，但查重与权威校验进入事务。
- [x] 新增记录先检查当前 attempt，同轮次重复复用原 id；更高轮次存在时拒绝。
- [x] 运行 Task 1 的测试，预期全部通过。

## Task 3: 接线与并发验收

**Files:** `tests/unit/scheduler/test_cron_dispatch_intent_service.py`、`tests/unit/scheduler/test_cron_execution_reconciliation.py`、新增 `tests/integrated/test_cron_execution_is_final.py`，设计文档与本计划。

- [x] 更新 Router 测试职责，验证原子服务被调用、没有事务外查重、初步拒绝不能插入。
- [x] 运行新测试与两组原有回归，预期无失败。
- [x] 提供显式隔离 MySQL 测试库的可选集成测试，验证双连接并发回报和回滚；没有测试库时跳过并明确报告，不能使用生产库。
- [x] 针对 schema/协议、数据更新、并发失败、范围与可维护性做四轮主线程自查；发现问题先补测试再修复。
- [x] 使用 verification-before-completion 核对最终测试、局部静态检查、diff 和文件范围。

## 基线与完成记录

基线：`test_cron_dispatch_intent_service.py` 与 `test_cron_execution_reconciliation.py` 共 77 项通过。GitNexus 对 Router、Scheduler record_execution、Monitor 初始化函数已做 upstream impact，均 LOW。

提交检查点仅保留为后续交付边界，本次按用户明确授权提交并推送到 origin/v1.0.0。完成后附准确验证结果与 MySQL 是否实际运行的说明。


## 实施结果（2026-10-14）

- 最终交付改为手工加列；Scheduler/Monitor 原 schema 不改动，SQL 仅加列，未执行目标数据库 DDL。
- Router 调用 `record_dispatch_execution()`；事务内锁定 intent、重验身份和轮次、查重、单条替换并插入。INSERT 复用原字段列表，通过数据库默认值得到 is_final=1。
- 四轮主线程自查：字段及历史语义；事务/取消/提交响应丢失；身份/迟到/重复回报；范围/回归与部署衔接。身份轮发现错租户历史行可能被替换，先新增失败测试，再增加 job/tenant 查询约束，重新验证通过。这些自查不是独立代理评审。
- 提交前最终 pytest：201 passed、3 skipped；跳过为未配置隔离 MySQL 库的并发及回滚测试。SQLite 行测试不证明 MySQL 行锁。
- 局部 flake8 使用项目配置的 E501 忽略和复杂度 15 门槛，通过；git diff --check 通过。
- 未修改统计/查询/Console/通知/trace；提交推送按用户后续授权执行，未执行生产 DDL。
- 原工作区未提交修改及暂存状态恢复；首次基线更新的一处 manager 冲突保留远端零偏移与本地日历转换。两次保护 stash 保留，便于回溯。
