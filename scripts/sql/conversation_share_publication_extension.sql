-- DESIGN DRAFT / NOT EXECUTED, updated 2026-10-30.
-- Inspect SHOW CREATE TABLE swe_chat_shares before reviewing this script.
-- Existing tokens, column definitions, collations, defaults and counters survive.
-- Apply once in an isolated review database after the six-table draft.
-- Not idempotent; apply to the final-source staging copy during the continuous
-- outage migration. Validate/backfill before installing the seven-table set.
-- Keep the original share table unchanged for rollback before new-version writes.
-- DDL implicitly commits. This script is not a rollbackable transaction.
-- Annotate all eight legacy fields using their actual definitions, including
-- deployment-specific character sets/collations/defaults. Indexes stay intact.
-- A changed/generated/default-expression legacy schema requires separate review.
-- Run with a fail-fast SQL client (no continue-on-error option).
-- PREPARE deliberately fails on an unexpected schema instead of changing it.
SET @conv_share_old_group_concat_max_len = @@SESSION.group_concat_max_len;
SET SESSION group_concat_max_len = 65535;
SELECT CASE WHEN COUNT(*) = 8 AND (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'swe_chat_shares'
) = 8 THEN CONCAT(
    'ALTER TABLE swe_chat_shares ',
    GROUP_CONCAT(CONCAT(
        'MODIFY COLUMN ', CHAR(96), c.COLUMN_NAME, CHAR(96), ' ', c.COLUMN_TYPE,
        CASE WHEN c.CHARACTER_SET_NAME IS NULL THEN ''
             ELSE CONCAT(' CHARACTER SET ', c.CHARACTER_SET_NAME,
                         ' COLLATE ', c.COLLATION_NAME) END,
        CASE WHEN c.IS_NULLABLE = 'YES' THEN ' NULL' ELSE ' NOT NULL' END,
        CASE WHEN c.COLUMN_DEFAULT IS NOT NULL
             THEN CONCAT(' DEFAULT ', QUOTE(c.COLUMN_DEFAULT))
             WHEN c.IS_NULLABLE = 'YES' THEN ' DEFAULT NULL' ELSE '' END,
        ' COMMENT ', QUOTE(m.column_comment)
    ) ORDER BY c.ORDINAL_POSITION SEPARATOR ', ')
) ELSE 'SCHEMA_REVIEW_REQUIRED_FOR_SWE_CHAT_SHARES' END
INTO @conv_share_comment_ddl
FROM information_schema.COLUMNS AS c
JOIN (
    SELECT 'token' AS column_name, 'varchar(128)' AS expected_type,
        'NO' AS expected_nullable, NULL AS expected_default,
        '不可预测的永久分享令牌，保留原值及现有主键语义' AS column_comment
    UNION ALL SELECT 'chat_id', 'varchar(128)', 'NO', NULL,
        '来源Chat外部标识；允许原Chat删除，不能据此阻止分享读取'
    UNION ALL SELECT 'creator_id', 'varchar(256)', 'NO', NULL,
        '分享创建者用户标识，保留旧记录原值'
    UNION ALL SELECT 'tenant_id', 'varchar(256)', 'NO', NULL,
        '旧分享所属租户原值；完整命名空间由publication元数据解析'
    UNION ALL SELECT 'snapshot_key', 'varchar(512)', 'NO', NULL,
        '原不可变快照定位键，迁移保留原值，不从中猜测scope'
    UNION ALL SELECT 'created_at', 'datetime(6)', 'NO', NULL,
        '分享创建UTC时间，微秒精度'
    UNION ALL SELECT 'access_count', 'bigint', 'NO', '0',
        '累计访问次数，保留现有计数更新与失败语义'
    UNION ALL SELECT 'last_access_at', 'datetime(6)', 'YES', NULL,
        '最近访问UTC时间；尚未访问时NULL'
) AS m ON m.column_name = c.COLUMN_NAME
WHERE c.TABLE_SCHEMA = DATABASE() AND c.TABLE_NAME = 'swe_chat_shares'
  AND c.EXTRA = '' AND c.GENERATION_EXPRESSION = ''
  AND LOWER(c.COLUMN_TYPE) = m.expected_type
  AND c.IS_NULLABLE = m.expected_nullable
  AND (c.COLUMN_DEFAULT <=> m.expected_default);
PREPARE conv_share_comment_stmt FROM @conv_share_comment_ddl;

ALTER TABLE swe_chat_shares
    ADD COLUMN publication_id BIGINT UNSIGNED NULL
        COMMENT '不可变分享正文所属publication内部ID；回填后由应用维护逻辑关联',
    ADD COLUMN byte_offset BIGINT UNSIGNED NULL
        COMMENT '分享正文在NAS bundle中的字节偏移；旧记录回填前为NULL',
    ADD COLUMN byte_length BIGINT UNSIGNED NULL
        COMMENT '分享正文字节长度；旧记录回填前为NULL，读取前检查范围',
    ADD KEY ix_swe_chat_shares_publication (publication_id),
    COMMENT = '永久会话分享索引、不可变快照定位及访问计数，独立于Chat生命周期';

EXECUTE conv_share_comment_stmt;
DEALLOCATE PREPARE conv_share_comment_stmt;
SET SESSION group_concat_max_len = @conv_share_old_group_concat_max_len;

-- A permanent share may outlive its Chat. Old NULL locators must be backfilled
-- and validated before cutover; no runtime file-only fallback is introduced.
