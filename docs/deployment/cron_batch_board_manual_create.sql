-- 新批调度看板增量手工DDL：先部署表，再升级Scheduler/SWE/Monitor。
-- 不由应用执行，不修改swe_cron_executions或任何旧队列。
-- 已有表不能靠CREATE IF NOT EXISTS升级，必须对照字段和索引。
CREATE TABLE IF NOT EXISTS swe_cron_managed_batch_members (
  batch_id VARCHAR(64) NOT NULL COMMENT '真实批触发回执中的批次标识',
  member_id VARCHAR(64) NOT NULL COMMENT '稳定逻辑成员标识，取初始执行ID',
  tenant_id VARCHAR(128) NOT NULL COMMENT '成员所属租户标识',
  source_id VARCHAR(128) NOT NULL COMMENT '成员来源系统标识',
  agent_id VARCHAR(128) NOT NULL COMMENT '成员所属智能体标识',
  job_id VARCHAR(128) NOT NULL COMMENT '成员任务定义标识',
  initial_execution_id VARCHAR(64) NOT NULL COMMENT '批次受理时关联的初始执行，可能来自普通入口',
  current_execution_id VARCHAR(64) NOT NULL COMMENT '最新业务尝试执行ID，不复制运行状态',
  business_attempt INT NOT NULL DEFAULT 1 COMMENT '业务尝试次数，运输恢复不增加',
  member_role VARCHAR(16) NOT NULL COMMENT '本批父或子角色：parent或child',
  dispatch_order INT NOT NULL COMMENT '受理时冻结的批内计划顺位',
  version BIGINT NOT NULL DEFAULT 1 COMMENT '当前尝试指针的并发版本',
  created_at DATETIME(6) NOT NULL COMMENT '关系首次登记时间，UTC',
  updated_at DATETIME(6) NOT NULL COMMENT '当前尝试更新时间，UTC',
  PRIMARY KEY (batch_id, member_id),
  UNIQUE KEY uk_managed_batch_identity (batch_id, tenant_id, source_id, agent_id, job_id),
  KEY idx_managed_batch_source (source_id, batch_id),
  KEY idx_managed_batch_initial (initial_execution_id),
  KEY idx_managed_batch_current (current_execution_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
COMMENT='新版批次逻辑成员及当前业务尝试关系表';

CREATE TABLE IF NOT EXISTS swe_cron_managed_dispatch_events (
  event_order BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '事件单调分页序号，不冒充业务开始顺序',
  event_id CHAR(64) NOT NULL COMMENT '持久事件标识',
  event_key CHAR(64) NOT NULL COMMENT '真实事实版本或人工请求的稳定去重摘要',
  tenant_id VARCHAR(128) NOT NULL COMMENT '事实所属租户标识',
  source_id VARCHAR(128) NOT NULL COMMENT '事实来源系统标识',
  agent_id VARCHAR(128) NOT NULL COMMENT '事实所属智能体标识',
  job_id VARCHAR(128) NOT NULL COMMENT '事实对应任务标识',
  execution_id VARCHAR(64) DEFAULT NULL COMMENT '对应执行ID，请求级审计可为空',
  batch_id VARCHAR(64) DEFAULT NULL COMMENT '已知关联批次，普通初始事实可为空',
  member_id VARCHAR(64) DEFAULT NULL COMMENT '稳定逻辑成员，请求级审计可为空',
  event_type VARCHAR(32) NOT NULL COMMENT '生命周期事实或人工重试决定类型',
  execution_version BIGINT DEFAULT NULL COMMENT '对应执行事实版本，请求级审计可为空',
  actor_id VARCHAR(128) NOT NULL COMMENT '真实操作或受理用户，不伪造接收人',
  occurred_at DATETIME(6) NOT NULL COMMENT '事实持久登记时间，UTC',
  details_json LONGTEXT NOT NULL COMMENT '安全字段及请求摘要响应，不保存原始异常或认证信息',
  PRIMARY KEY (event_id),
  UNIQUE KEY uk_managed_event_key (event_key),
  UNIQUE KEY uk_managed_event_order (event_order),
  KEY idx_managed_event_batch (source_id, batch_id, event_order),
  KEY idx_managed_event_execution (execution_id, event_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
COMMENT='新版批派发生命周期事实及人工重试审计事件表';
