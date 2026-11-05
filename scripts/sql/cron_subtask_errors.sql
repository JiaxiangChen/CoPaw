-- Run once before deploying the updated monitor service.
ALTER TABLE swe_cron_subtasks
    ADD COLUMN error_type VARCHAR(128) DEFAULT NULL COMMENT '错误类型',
    ADD COLUMN error_reason VARCHAR(2048) DEFAULT NULL COMMENT '错误原因';
