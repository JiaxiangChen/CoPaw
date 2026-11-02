-- Run after migrate_tenant_init_source_bbk.sql. Safe to rerun.
-- Supports tenant queries filtered by source_id and bbk_id.
SET @tenant_source_bbk_index_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'swe_tenant_init_source'
      AND INDEX_NAME = 'idx_source_bbk'
);

SET @tenant_source_bbk_index_sql = IF(
    @tenant_source_bbk_index_exists = 0,
    'ALTER TABLE swe_tenant_init_source ADD INDEX idx_source_bbk (source_id, bbk_id)',
    'SELECT 1'
);

PREPARE tenant_source_bbk_index_stmt FROM @tenant_source_bbk_index_sql;
EXECUTE tenant_source_bbk_index_stmt;
DEALLOCATE PREPARE tenant_source_bbk_index_stmt;
