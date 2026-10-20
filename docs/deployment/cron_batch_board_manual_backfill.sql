-- 仅回填已有“新版链路”的真实批触发成员，不迁移 legacy/jobs.json，不补造历史事件。
-- 要求 MySQL 8.0.21+（JSON_TABLE），同一会话逐段执行；不支持直接套到 MySQL 5.7。
-- 维护窗口暂停该来源的新触发/人工重试，确认没有在途的新批受理事务，备份并先执行两张表 CREATE。
-- 每个来源执行一次；值由运维明确替换，不能用空值代表全库。
SET @cron_board_source = '请替换为真实source_id';

-- 临时表只在当前会话存在，不是新增业务表。
DROP TEMPORARY TABLE IF EXISTS cron_board_backfill_candidates;
CREATE TEMPORARY TABLE cron_board_backfill_candidates AS
SELECT c.batch_id, c.tenant_id AS root_tenant_id, c.source_id AS root_source_id,
       c.agent_id AS root_agent_id, c.job_id AS root_job_id,
       j.member_order - 1 AS dispatch_order, j.tenant_id, j.source_id, j.agent_id, j.job_id,
       j.execution_id, c.created_at,
       CASE WHEN r.execution_id IS NULL OR d.management_owner <> 'scheduler'
         OR d.job_id IS NULL OR s.job_id IS NULL OR r.kind NOT IN ('ordinary','batch')
         OR p.job_id IS NULL OR p.management_owner <> 'scheduler'
         OR r.scheduled_fire_at IS NULL
         OR COALESCE(JSON_UNQUOTE(JSON_EXTRACT(r.dispatch_meta_json, '$.protocol')), '') <> 'scheduler_managed_v1'
         OR JSON_LENGTH(JSON_EXTRACT(c.members_json,CONCAT('$[',j.member_order-1,'].identity')))<>4
         OR BINARY j.source_id <> BINARY c.source_id
         THEN 1 ELSE 0 END AS invalid_relation
FROM swe_cron_managed_trigger_receipts c
JOIN JSON_TABLE(c.members_json, '$[*]' COLUMNS (
  member_order FOR ORDINALITY,
  execution_id VARCHAR(64) PATH '$.execution_id' ERROR ON EMPTY ERROR ON ERROR,
  tenant_id VARCHAR(128) PATH '$.identity[0]' ERROR ON EMPTY ERROR ON ERROR,
  source_id VARCHAR(128) PATH '$.identity[1]' ERROR ON EMPTY ERROR ON ERROR,
  agent_id VARCHAR(128) PATH '$.identity[2]' ERROR ON EMPTY ERROR ON ERROR,
  job_id VARCHAR(128) PATH '$.identity[3]' ERROR ON EMPTY ERROR ON ERROR
)) j
LEFT JOIN swe_cron_jobs_v2 p ON BINARY p.tenant_id=BINARY c.tenant_id
  AND BINARY p.source_id=BINARY c.source_id AND BINARY p.agent_id=BINARY c.agent_id AND BINARY p.job_id=BINARY c.job_id
LEFT JOIN swe_cron_execution_runs r ON BINARY r.execution_id=BINARY j.execution_id
  AND BINARY r.tenant_id=BINARY j.tenant_id AND BINARY r.source_id=BINARY j.source_id
  AND BINARY r.agent_id=BINARY j.agent_id AND BINARY r.job_id=BINARY j.job_id
LEFT JOIN swe_cron_jobs_v2 d ON BINARY d.tenant_id=BINARY j.tenant_id
  AND BINARY d.source_id=BINARY j.source_id AND BINARY d.agent_id=BINARY j.agent_id AND BINARY d.job_id=BINARY j.job_id
LEFT JOIN swe_cron_job_states s ON BINARY s.tenant_id=BINARY j.tenant_id
  AND BINARY s.source_id=BINARY j.source_id AND BINARY s.agent_id=BINARY j.agent_id AND BINARY s.job_id=BINARY j.job_id
WHERE c.source_id=@cron_board_source AND BINARY c.source_id=BINARY @cron_board_source
  AND @cron_board_source<>'' AND @cron_board_source<>'请替换为真实source_id'
  AND c.kind='batch' AND c.status='accepted' AND c.batch_id IS NOT NULL;

-- 以下检查必须均为0行；任意异常停止，不能把缺失成员当作零成员或按kind=batch猜测归属。
SELECT * FROM cron_board_backfill_candidates WHERE invalid_relation<>0;
SELECT batch_id, execution_id, COUNT(*) AS duplicate_count
FROM cron_board_backfill_candidates GROUP BY batch_id,execution_id HAVING COUNT(*)>1;
SELECT batch_id,tenant_id,source_id,agent_id,job_id,COUNT(*) AS duplicate_count
FROM cron_board_backfill_candidates GROUP BY batch_id,tenant_id,source_id,agent_id,job_id HAVING COUNT(*)>1;
SELECT b.batch_id,b.execution_id FROM cron_board_backfill_candidates b
JOIN swe_cron_managed_batch_members m ON m.batch_id=b.batch_id AND m.member_id=b.execution_id
WHERE BINARY m.tenant_id<>BINARY b.tenant_id OR BINARY m.source_id<>BINARY b.source_id
  OR BINARY m.agent_id<>BINARY b.agent_id OR BINARY m.job_id<>BINARY b.job_id
  OR BINARY m.initial_execution_id<>BINARY b.execution_id;

START TRANSACTION;
-- 先单独计算闸门，避免 MySQL 临时表同语句引用两次的 Can't reopen table 限制。
SET @cron_board_invalid = (SELECT COUNT(*) FROM cron_board_backfill_candidates WHERE invalid_relation<>0);
-- 只 INSERT 缺失关系，不覆盖已发生重试的 current_execution_id/business_attempt/version。
INSERT INTO swe_cron_managed_batch_members (
  batch_id,member_id,tenant_id,source_id,agent_id,job_id,initial_execution_id,current_execution_id,
  business_attempt,member_role,dispatch_order,version,created_at,updated_at
)
SELECT b.batch_id,b.execution_id,b.tenant_id,b.source_id,b.agent_id,b.job_id,b.execution_id,b.execution_id,
  1,CASE WHEN BINARY b.tenant_id=BINARY b.root_tenant_id AND BINARY b.source_id=BINARY b.root_source_id
    AND BINARY b.agent_id=BINARY b.root_agent_id AND BINARY b.job_id=BINARY b.root_job_id THEN 'parent' ELSE 'child' END,
  b.dispatch_order,1,b.created_at,UTC_TIMESTAMP(6)
FROM cron_board_backfill_candidates b
LEFT JOIN swe_cron_managed_batch_members m ON m.batch_id=b.batch_id AND m.member_id=b.execution_id
WHERE m.member_id IS NULL
  AND @cron_board_invalid=0;

-- 必须0行后再 COMMIT；有行则 ROLLBACK 并排查，禁止忽略错误继续提交。
SELECT c.batch_id,JSON_LENGTH(c.members_json) AS expected_count,COUNT(m.member_id) AS actual_count
FROM swe_cron_managed_trigger_receipts c
LEFT JOIN swe_cron_managed_batch_members m ON m.batch_id=c.batch_id
WHERE c.source_id=@cron_board_source AND BINARY c.source_id=BINARY @cron_board_source
  AND c.kind='batch' AND c.status='accepted' AND c.batch_id IS NOT NULL
GROUP BY c.receipt_id HAVING expected_count<>actual_count;
-- 人工确认上述结果后执行 COMMIT；不要把本文件整段无检查地运行。
-- COMMIT;
-- ROLLBACK;
