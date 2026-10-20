-- 仅供数据库负责人逐条选择执行，不是可整文件无条件运行的升级脚本。
-- 先运行 cron_phase2_schema_audit.sql 并保存 SHOW CREATE TABLE / SHOW INDEX。
-- 同名列/索引存在时跳过；不同类型/默认值/索引定义不能简单跳过，需单独核对。
-- 缺少整张新版表：使用 cron_phase2_manual_create.sql 中对应 CREATE。
-- 不在本文件补写任何执行成功、管理归属或历史执行水位。
-- 不修改原 swe_cron_executions 表结构。
-- 四个新增查询副本列见 cron_profile_columns_manual_upgrade.sql，须在发布新版写入代码前补齐。
-- 已有定义回填见 cron_profile_columns_manual_backfill.sql，按预览结果人工决定执行。

-- 一、此前执行登记草案缺少整轮收敛字段时，分别补齐，未知结果保持 NULL。
ALTER TABLE swe_cron_execution_runs
  ADD COLUMN settled_status VARCHAR(16) DEFAULT NULL
  COMMENT 'Scheduler整轮派发收敛结果，空表示异步终态尚未确认';
ALTER TABLE swe_cron_execution_runs
  ADD COLUMN settled_at DATETIME(6) DEFAULT NULL
  COMMENT 'Scheduler观察到整轮结束的时间，UTC';
ALTER TABLE swe_cron_execution_runs
  ADD COLUMN settled_error_code VARCHAR(128) DEFAULT NULL
  COMMENT '整轮收敛错误码，不覆盖SWE主执行结果';
ALTER TABLE swe_cron_execution_runs
  ADD INDEX idx_managed_execution_settlement (settled_status, status, queue_order);
ALTER TABLE swe_cron_execution_runs
  ADD INDEX idx_managed_execution_feedback (source_id, provider_id, model_id, settled_at);

-- 二、第一阶段状态表缺少未开始队列失效水位时补齐。
-- 默认零只用于此前没有新版执行记录的环境；有旧草案执行记录时，先停新版入口，
-- 核对 state_version 与每条 enqueued_state_version，不得靠默认零复活原失效队列。
ALTER TABLE swe_cron_job_states
  ADD COLUMN queue_invalidation_version BIGINT NOT NULL DEFAULT 0
  COMMENT '未开始队列失效水位，入队版本小于本值时跳过';

-- 三、原草案状态时间精度不足时，确认原列可空后逐项修改。
-- 必须与保存同一暂停事实的定义/日志/执行时间保持微秒一致；不修改时间值及时区。
ALTER TABLE swe_cron_job_states
  MODIFY COLUMN auto_paused_at DATETIME(6) DEFAULT NULL COMMENT '首次自动暂停时间，UTC';
ALTER TABLE swe_cron_job_states
  MODIFY COLUMN management_updated_at DATETIME(6) DEFAULT NULL COMMENT '管理字段更新时间，UTC';
ALTER TABLE swe_cron_job_states
  MODIFY COLUMN runtime_updated_at DATETIME(6) DEFAULT NULL COMMENT '执行运行字段更新时间，UTC';

-- 四、更早草案若缺 queue_order、dispatch_order、provider_id、model_id、
-- enqueued_state_version、started_order、not_before_at，不能盲目整批 ADD 并填零。
-- 空表：按完整 CREATE 对照补列和索引，queue_order 必须为带唯一索引的自增列。
-- 非空表：暂停新版触发/提交并保留运行结果写入，导出相关定义、状态、执行及日志；
-- 按各执行自己的冻结快照、既有状态和实际入队事实设计一次性回填后再加非空约束。
-- 不从当前最新定义反推历史模型、工作流版本或入队水位，不用当前时间填历史顺序。
-- 本次没有目标库真实结构/数据，因此不提供臆造回填，也不 DROP/TRUNCATE 任何表。

-- 五、任意第一阶段基础列缺失、类型不一致、旧表既有依赖缺失，均先停止上线。
-- 将实际 SHOW CREATE TABLE 与完整 CREATE 对照交由数据库负责人确认，
-- 不把 CREATE IF NOT EXISTS 的成功返回当作旧表升级成功。
