-- 新批看板的既有回执表索引升级；不修改旧执行历史、旧队列或旧批次。
-- MySQL 8.0.21+；先备份并检查 SHOW INDEX，同名/等价索引已有则不要重复执行 ALTER。
SHOW INDEX FROM swe_cron_managed_trigger_receipts;

-- 必须为0行；不为0时先排查，不能删除重复回执或任意保留一条。
SELECT batch_id, COUNT(*) AS duplicate_count
FROM swe_cron_managed_trigger_receipts WHERE batch_id IS NOT NULL
GROUP BY batch_id HAVING COUNT(*) > 1;

-- 普通触发 batch_id 为 NULL；MySQL 唯一索引允许多个 NULL。
ALTER TABLE swe_cron_managed_trigger_receipts
  ADD UNIQUE KEY uk_managed_receipt_batch (batch_id),
  ADD KEY idx_managed_receipt_board (source_id, kind, status, logical_fire_at, batch_id);
