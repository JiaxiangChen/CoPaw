-- Apply before deploying the Scheduler writer. Run once on the execution DB.
-- Existing records all read as 1. Do not backfill or reset existing markers.
-- Schema initializers do not add this column; new databases need this SQL too.
ALTER TABLE swe_cron_executions
    ADD COLUMN is_final TINYINT(1) NOT NULL DEFAULT 1
    COMMENT '是否为当前有效执行记录：1有效，0已被后续执行替代'
    AFTER dispatch_attempt;
