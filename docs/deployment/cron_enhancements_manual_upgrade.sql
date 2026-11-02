-- 新版定时任务四项优化：只供数据库负责人核对后逐条手工执行。
-- 已存在同名列或索引时不要重复执行；先保存 SHOW CREATE TABLE / SHOW INDEX。
-- 不创建新业务表，不改 swe_cron_executions，不回填历史结果或重注册旧定时器。
-- 应先执行结构变更，再部署本次 Scheduler/SWE；程序不会自动运行此文件。

ALTER TABLE swe_cron_jobs_v2
  ADD COLUMN run_start_date DATE DEFAULT NULL
  COMMENT '允许运行开始日期，北京时间，包含当天，空表示不限制',
  ADD COLUMN run_end_date DATE DEFAULT NULL
  COMMENT '允许运行结束日期，北京时间，包含当天，空表示不限制';

ALTER TABLE swe_cron_job_states
  ADD COLUMN date_window_paused TINYINT(1) NOT NULL DEFAULT 0
  COMMENT '日期区间暂停标记，不覆盖人工或其他暂停原因',
  ADD COLUMN date_window_sync_required TINYINT(1) NOT NULL DEFAULT 0
  COMMENT '日期配置变更或平台同步失败后仍需同步的标记';

ALTER TABLE swe_cron_execution_runs
  ADD COLUMN dispatch_lane VARCHAR(16) NOT NULL DEFAULT 'normal'
  COMMENT '派发通道：normal正常，urgent已结束批次异常重跑加急';

-- 以下索引先核对现有等价索引、MySQL版本、字符集和执行计划，再决定补建。
ALTER TABLE swe_cron_jobs_v2
  ADD INDEX idx_managed_definition_source_dates (source_id, management_owner, deleted_at);
ALTER TABLE swe_cron_managed_trigger_receipts
  ADD INDEX idx_managed_trigger_batch (batch_id),
  ADD INDEX idx_managed_trigger_received (source_id, kind, status, created_at, batch_id);
ALTER TABLE swe_cron_execution_runs
  ADD INDEX idx_managed_execution_priority (source_id, provider_id, model_id, dispatch_lane, status, queue_order);
