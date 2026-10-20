-- 人工升级：仅在四列缺失时选择执行，已有同名列需核对类型，不能重复 ADD。
-- 请先备份并保存 SHOW CREATE TABLE swe_cron_jobs_v2。
-- 先加列，再发布四列双写代码，最后按配套回填材料处理已有行。
-- 本文件不会被应用自动执行，不修改旧表、状态表或任务定义 JSON。
ALTER TABLE swe_cron_jobs_v2
  ADD COLUMN tenant_name VARCHAR(255) NOT NULL DEFAULT ''
  COMMENT '用户姓名，与完整定义同步保存';
ALTER TABLE swe_cron_jobs_v2
  ADD COLUMN bbk_id VARCHAR(128) NOT NULL DEFAULT ''
  COMMENT '所属机构或分行号，与完整定义同步保存';
ALTER TABLE swe_cron_jobs_v2
  ADD COLUMN skill_ids VARCHAR(200) NOT NULL DEFAULT ''
  COMMENT '绑定技能标识，逗号分隔，与完整定义同步保存';
ALTER TABLE swe_cron_jobs_v2
  ADD COLUMN workflow_binding_id VARCHAR(128) DEFAULT NULL
  COMMENT '工作流绑定标识，与完整定义同步保存';
