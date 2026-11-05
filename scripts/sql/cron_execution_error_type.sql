-- Run once before deploying the updated monitor service.
ALTER TABLE swe_cron_executions
    ADD COLUMN error_type VARCHAR(2048) DEFAULT NULL COMMENT '子任务错误类型，逗号分隔';
