# 整组暂停同步标识唯一性修复实施计划

> **For agentic workers:** 按仓库 AGENTS.md 由主线程顺序执行 executing-plans 和 TDD；不派发子代理。自检不等于独立评审。

**Goal:** 修复带子任务整组暂停时违反 sync_operation_id 唯一约束导致的 1062/503。

**Architecture:** 保留整组 operation_id；每个对象的 sync_operation_id 使用现有模式切换的 UUID5 规则，由整组操作与完整任务身份生成。保留数据库唯一索引、事务和异步平台同步流程。

**Tech Stack:** Python 3.10+、pytest、SQLite 约束回归；生产为 MySQL。

**Spec:** 已确认的暂停故障修复要求；结构依据为 docs/deployment/cron_phase2_manual_create.sql 的 uk_managed_mode_sync。

## 全局约束

- 不删除索引、清理生产数据、修改接口或变更调度语义。
- 保留既有未提交的 tracking_timeline.py 显式导入，不将其当作本次新增修改。
- 不提交、推送、重打包或修改业务数据库。
- GitNexus 当前 worktree 未注册，影响分析已尝试但不可用；以源码及测试验证，不能宣称图谱检查通过。

## Task 1：约束、修复与验证

**Files:**
- Modify: tests/unit/scheduler/test_managed_cron_dispatch_state.py 的 ModeDatabase。
- Modify: tests/unit/scheduler/test_managed_cron_group_pause.py。
- Modify: scheduler/src/scheduler/app/services/cron/managed/pause_group.py 的 _pause_member。

**Interfaces:** 继续沿用 submit、process、get、retry；输入、operation 包装及完整任务身份均不变。

- [x] 为测试表增加 `UNIQUE(sync_operation_id)`，与实际 DDL 一致。
- [x] 新增参数化回归：正常父子组、子任务此前已暂停；每对象同步 ID 唯一，相同 request_id 重放复用原记录。
- [x] 运行暂停测试，旧代码 6 个失败、1 个通过，错误为 `UNIQUE constraint failed: swe_cron_managed_mode_items.sync_operation_id`；HTTP 回归复现 503。
- [x] 最小修复：导入 `NAMESPACE_URL, uuid5`，同步 ID 写为 `str(uuid5(NAMESPACE_URL, canonical([operation["operation_id"], key])))`。
- [x] 暂停和调度方式 12 个测试通过（5.31 秒），包括平台失败续办、子任务单独暂停及 HTTP operation 查询；新增断言验证续办保持同步 ID 不变。
- [x] `tests/unit/scheduler` 全目录 781 个测试通过（50.16 秒）；226 条既有 SQLite datetime adapter 弃用警告，无失败。
- [x] 本次 Python 文件的 pre-commit 全部通过，包括复杂度上限 15、mypy、Black、flake8、pylint；格式工具整理过新增测试，最终复跑通过。diff 未修改 DDL、前端或生产数据。
- [x] 停留于本地修改，未提交、推送或重打包；保留原有 anext 导入。评审为主线程自检，未使用子代理；真实行内 MySQL/平台尚需部署验收。

测试命令统一在 WSL 使用项目虚拟环境：

```text
PYTHONPATH=scheduler/src:monitor/src:src /mnt/d/Projects/CoPaw/venv/bin/python -m pytest tests/unit/scheduler/test_managed_cron_group_pause.py tests/unit/scheduler/test_managed_cron_dispatch_state.py -q --tb=line
PYTHONPATH=scheduler/src:monitor/src:src /mnt/d/Projects/CoPaw/venv/bin/python -m pytest tests/unit/scheduler -q --tb=line
```
