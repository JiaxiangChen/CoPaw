-- 四列人工回填：先执行配套升级 SQL 并发布四列双写代码。
-- 请备份数据，在同一数据库连接中按段执行，先检查预览，再决定执行 UPDATE。
-- 建议避开任务管理高峰。大表应由 DBA 按完整身份范围分批处理。
-- 仅从本行 spec_json 回填，不读取旧表，不改变版本、归属、排程或运行状态。
-- 异常行整行跳过，不截断。创建临时快照后定义变化的行也跳过，可再次执行。
-- 临时表仅在当前连接存在；连接池/工具不得把各语句分派到不同连接。
DROP TEMPORARY TABLE IF EXISTS cron_profile_backfill_candidates;
CREATE TEMPORARY TABLE cron_profile_backfill_candidates AS
SELECT tenant_id, source_id, agent_id, job_id,
       SHA2(spec_json, 256) AS source_spec_hash,
       JSON_VALID(spec_json) AS valid_json,
       JSON_TYPE(IF(JSON_VALID(spec_json), spec_json, '{}')) AS spec_type,
       JSON_TYPE(JSON_EXTRACT(IF(JSON_VALID(spec_json), spec_json, '{}'), '$.tenant_name')) AS tenant_name_type,
       JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(spec_json), spec_json, '{}'), '$.tenant_name')) AS tenant_name,
       JSON_TYPE(JSON_EXTRACT(IF(JSON_VALID(spec_json), spec_json, '{}'), '$.bbk_id')) AS bbk_id_type,
       JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(spec_json), spec_json, '{}'), '$.bbk_id')) AS bbk_id,
       JSON_TYPE(JSON_EXTRACT(IF(JSON_VALID(spec_json), spec_json, '{}'), '$.skill_ids')) AS skill_ids_type,
       JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(spec_json), spec_json, '{}'), '$.skill_ids')) AS skill_ids,
       JSON_TYPE(JSON_EXTRACT(IF(JSON_VALID(spec_json), spec_json, '{}'), '$.workflow_binding_id')) AS workflow_binding_id_type,
       JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(spec_json), spec_json, '{}'), '$.workflow_binding_id')) AS workflow_binding_id
FROM swe_cron_jobs_v2;

ALTER TABLE cron_profile_backfill_candidates
  ADD COLUMN can_backfill TINYINT NOT NULL DEFAULT 0;
UPDATE cron_profile_backfill_candidates
SET can_backfill = 1
WHERE valid_json = 1 AND spec_type = 'OBJECT'
  AND COALESCE(NULLIF(tenant_name_type, 'NULL'), 'STRING') = 'STRING'
  AND COALESCE(NULLIF(bbk_id_type, 'NULL'), 'STRING') = 'STRING'
  AND COALESCE(NULLIF(skill_ids_type, 'NULL'), 'STRING') = 'STRING'
  AND COALESCE(NULLIF(workflow_binding_id_type, 'NULL'), 'STRING') = 'STRING'
  AND (tenant_name_type IS NULL OR tenant_name_type = 'NULL' OR CHAR_LENGTH(tenant_name) <= 255)
  AND (bbk_id_type IS NULL OR bbk_id_type = 'NULL' OR CHAR_LENGTH(bbk_id) <= 128)
  AND (skill_ids_type IS NULL OR skill_ids_type = 'NULL' OR CHAR_LENGTH(skill_ids) <= 200)
  AND (workflow_binding_id_type IS NULL OR workflow_binding_id_type = 'NULL' OR CHAR_LENGTH(workflow_binding_id) <= 128);

-- 预览：异常行只展示身份，不输出任务正文或完整定义。需人工核对后重新处理。
SELECT tenant_id, source_id, agent_id, job_id,
       'invalid_json_or_profile_type_or_length' AS skip_reason
FROM cron_profile_backfill_candidates WHERE can_backfill = 0;
SELECT can_backfill, COUNT(*) AS row_count
FROM cron_profile_backfill_candidates GROUP BY can_backfill;

-- 确认后执行。使用完整身份关联，且定义未变化才允许覆盖查询副本。
UPDATE swe_cron_jobs_v2 d
JOIN cron_profile_backfill_candidates c
  ON d.tenant_id = c.tenant_id AND d.source_id = c.source_id
 AND d.agent_id = c.agent_id AND d.job_id = c.job_id
SET d.tenant_name = CASE WHEN c.tenant_name_type = 'STRING' THEN c.tenant_name ELSE '' END,
    d.bbk_id = CASE WHEN c.bbk_id_type = 'STRING' THEN c.bbk_id ELSE '' END,
    d.skill_ids = CASE WHEN c.skill_ids_type = 'STRING' THEN c.skill_ids ELSE '' END,
    d.workflow_binding_id = CASE WHEN c.workflow_binding_id_type = 'STRING' THEN NULLIF(c.workflow_binding_id, '') ELSE NULL END
WHERE c.can_backfill = 1 AND SHA2(d.spec_json, 256) = c.source_spec_hash;

-- 定义变化而跳过的行，重新执行本文件即可处理，不覆盖并发更新。
SELECT d.tenant_id, d.source_id, d.agent_id, d.job_id,
       'definition_changed_retry_backfill' AS skip_reason
FROM swe_cron_jobs_v2 d
JOIN cron_profile_backfill_candidates c
  ON d.tenant_id = c.tenant_id AND d.source_id = c.source_id
 AND d.agent_id = c.agent_id AND d.job_id = c.job_id
WHERE c.can_backfill = 1 AND SHA2(d.spec_json, 256) <> c.source_spec_hash;
DROP TEMPORARY TABLE cron_profile_backfill_candidates;
