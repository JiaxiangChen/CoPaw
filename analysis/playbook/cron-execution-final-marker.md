# 执行记录 is_final 排查

`swe_cron_executions.is_final` 默认 1，表示记录尚未被后续批调度执行替代，不代表成功或已经停止重试。普通任务每次写入均为 1；异步状态和通知回写不修改该字段。

批调度完整回报由 SWE 发到 Scheduler `/scheduler/cron/execution`，由 `ExecutionSyncService.record_dispatch_execution()` 在同一事务内锁定 intent、校验轮次、查重、更新最新旧记录为 0、插入新记录。自动和手动重试共用此入口；仅重新入队不会改变旧标记。

排查多条最终记录时先核对 batch/intent/attempt：

- 历史数据加列后全部为 1，不清洗。历史三条全为 1 后新增第四条，会得到 1、1、0、1，这是既定兼容行为。
- 新产生的同轮次重复回报应返回同一 execution_id，不增加记录。
- 旧轮次回报应被当前 intent 的 attempt 校验拒绝，不覆盖新轮次。
- 新旧 Scheduler 写入代码混跑，或完整批调度结果绕过 Scheduler 直接写 Monitor，会绕过事务规则；部署时应完成 Scheduler 写入切换。

加列说明见 `scripts/sql/cron_execution_is_final.sql`。两个服务的原 schema 不自动添加该字段；现有库和新建库都需先手工加列再发布写入代码，不用历史 UPDATE 或批量重置标记。

运营看板及定时任务统计/执行详情的执行记录查询统一限定 `is_final=1`，包括分页总数、导出和下钻；被替代的单条执行 ID 返回不存在。用户排行 `cron_success` 要求 `status='success' AND async_status='success'`。有效记录仍可能运行中；跨日替代会改变历史日期范围统计。独立点击指标不随执行标记清理。

索引不能仅因新增低基数字段就追加：先核对实际 `SHOW INDEX`、标记分布和代表 SQL 的 `EXPLAIN`，再比较 `(job_id,is_final,actual_time)` 与 `(is_final,actual_time)` 的扫描量、排序和写入成本；读趋势必要时评估 `(is_final,read_at)`。本次查询变更不自动执行 DDL。统计回归见 `tests/unit/monitor/test_cron_final_analytics.py`，完整设计见 `docs/superpowers/specs/2026-10-19-cron-final-analytics-design.md`。

回归入口为 `tests/unit/scheduler/test_cron_execution_is_final.py`。MySQL 并发测试为 `tests/integrated/test_cron_execution_is_final.py`：仅允许空的 `codex_test_*` 数据库，显式配置 `COPAW_CRON_TEST_MYSQL_DATABASE/HOST/USER`，可选 `PORT/PASSWORD`。该测试会创建和清理 execution、intent 测试表；未配置时跳过，SQLite 用例不证明 MySQL 行锁。
