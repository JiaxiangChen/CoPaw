-- 第二阶段结构只读核对，不执行建表、更新或业务数据扫描。
-- 在 SWE、Scheduler、Monitor 实际共用的目标数据库中执行；空缺列结果才是通过。
SELECT DATABASE() AS current_database, VERSION() AS database_version;

-- 第一项：检查新后台启动的16张表，以及智拓Workflow使用的2张既有绑定表。
SELECT required.table_name, required.column_name AS missing_column
FROM (
  SELECT 'swe_cron_jobs_v2' AS table_name, 'tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'agent_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'scope_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'name' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'tenant_name' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'bbk_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'skill_ids' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'workflow_binding_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'task_type' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'enabled' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'cron_expr' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'timezone' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'spec_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'offset_minutes' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'offset_window_hours' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'enable_offset' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'management_owner' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'creator_user_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'definition_version' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'deleted_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'parent_tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'parent_source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'parent_agent_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'parent_job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'original_cron_expr' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'job_origin' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'plan_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'source_definition_version' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'created_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'updated_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'imported_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'source_observed_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs_v2' AS table_name, 'migration_batch_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'agent_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'task_chat_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'task_session_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'ordinary_external_job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'batch_external_job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'dispatch_state' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'pause_reason' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'auto_paused_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'next_run_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'next_run_times_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'last_status' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'last_run_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'last_prefetch_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'last_error' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'has_scheduled_result' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'latest_scheduled_preview' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'last_scheduled_run_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'unread_execution_count' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'management_status' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'management_version' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'state_version' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'queue_invalidation_version' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'state_source' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'management_updated_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'created_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'updated_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'state_observed_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_job_states' AS table_name, 'runtime_updated_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'operation_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'active_key' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'agent_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'actor_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'action' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'request_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'request_hash' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'expected_version' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'payload_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'progress_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'status' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'stage' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'error_code' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'lease_owner' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'lease_until' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'version' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'created_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_registration_journal' AS table_name, 'updated_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'execution_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'execution_key' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'queue_order' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'dispatch_order' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'agent_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'scope_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'actor_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'definition_version' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'provider_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'model_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'enqueued_state_version' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'started_order' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'kind' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'trigger_key' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'scheduled_fire_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'not_before_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'spec_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'dispatch_meta_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'status' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'dispatch_attempt' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'dispatch_owner' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'dispatch_lease_until' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'run_owner' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'run_lease_until' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'accepted_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'started_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'finished_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'history_execution_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'output_preview' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'trace_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'error_code' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'result_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'version' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'created_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'updated_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'settled_status' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'settled_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_execution_runs' AS table_name, 'settled_error_code' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'created_order' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'task_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'active_key' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'agent_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'actor_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'actor_is_manager' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'request_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'request_hash' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'source_definition_version' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'payload_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'tenant_count' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'status' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'sync_status' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'error_code' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'lease_owner' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'lease_until' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'version' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'created_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_tasks' AS table_name, 'updated_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'task_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'index_no' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'agent_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'expected_definition_version' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'operation' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'status' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'plan_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'result_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'cron_expr' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'timezone' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'offset_minutes' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'warning' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'lease_owner' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'lease_until' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'version' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'created_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_broadcast_items' AS table_name, 'updated_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_dispatch_controls' AS table_name, 'tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_dispatch_controls' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_dispatch_controls' AS table_name, 'agent_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_dispatch_controls' AS table_name, 'job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_dispatch_controls' AS table_name, 'state' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_dispatch_controls' AS table_name, 'version' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_dispatch_controls' AS table_name, 'operation_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_dispatch_controls' AS table_name, 'sync_status' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_dispatch_controls' AS table_name, 'error_code' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_mode_items' AS table_name, 'operation_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_mode_items' AS table_name, 'index_no' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_mode_items' AS table_name, 'tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_mode_items' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_mode_items' AS table_name, 'agent_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_mode_items' AS table_name, 'job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_mode_items' AS table_name, 'expected_definition_version' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_mode_items' AS table_name, 'sync_operation_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_mode_items' AS table_name, 'status' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_mode_items' AS table_name, 'error_code' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'receipt_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'callback_key' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'agent_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'external_job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'operation_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'kind' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'trigger_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'logical_fire_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'physical_fire_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'batch_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'status' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'response_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'members_json' AS column_name
  UNION ALL
  SELECT 'swe_cron_managed_trigger_receipts' AS table_name, 'created_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'name' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'tenant_name' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'bbk_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'enabled' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'task_type' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'cron_expr' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'timezone' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'channel' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'target_user_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'target_session_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'timeout_seconds' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'max_concurrency' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'misfire_grace_seconds' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'text_content' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'request_input' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'creator_user_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'task_chat_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'task_session_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'plan_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'meta' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'skill_ids' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'workflow_binding_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'job_origin' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'status' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'pause_reason' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'deleted_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'created_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_jobs' AS table_name, 'updated_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'id' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'job_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'job_name' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'tenant_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'scheduled_time' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'actual_time' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'end_time' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'duration_ms' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'status' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'async_status' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'error_message' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'instance_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'executor_leader' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'is_manual' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'trace_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'session_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'input_snapshot' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'output_preview' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'meta' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'notification_status' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'notification_due_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'notification_timezone' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'is_read' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'read_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_executions' AS table_name, 'created_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_scope_leases' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_scope_leases' AS table_name, 'provider_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_scope_leases' AS table_name, 'model_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_scope_leases' AS table_name, 'lock_owner' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_scope_leases' AS table_name, 'locked_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_scope_leases' AS table_name, 'lease_expires_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_scope_leases' AS table_name, 'heartbeat_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'worker_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'provider_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'model_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'strategy_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'previous_workers' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'baseline_workers' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'min_workers' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'max_workers' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'effective_workers' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'pending_count' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'claimed_count' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'running_count' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'success_count' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'failure_count' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'error_rate' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'matched_rule' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'avg_latency_ms' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'decision_reason' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_capacity' AS table_name, 'created_at' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_intents' AS table_name, 'id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_intents' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_intents' AS table_name, 'provider_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_intents' AS table_name, 'model_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_intents' AS table_name, 'status' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_model_worker_policy' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_model_worker_policy' AS table_name, 'provider_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_model_worker_policy' AS table_name, 'model_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_model_worker_policy' AS table_name, 'default_strategy_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_model_worker_policy' AS table_name, 'strategy_schedule' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_model_worker_policy' AS table_name, 'enabled' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_strategy' AS table_name, 'strategy_id' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_strategy' AS table_name, 'min_workers' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_strategy' AS table_name, 'baseline_workers' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_strategy' AS table_name, 'max_workers' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_strategy' AS table_name, 'adjust_interval_seconds' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_strategy' AS table_name, 'feedback_window_seconds' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_strategy' AS table_name, 'stale_execution_seconds' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_strategy' AS table_name, 'error_rate_rules' AS column_name
  UNION ALL
  SELECT 'swe_cron_dispatch_worker_strategy' AS table_name, 'enabled' AS column_name
  UNION ALL
  SELECT 'swe_workflow_bindings' AS table_name, 'binding_id' AS column_name
  UNION ALL
  SELECT 'swe_workflow_bindings' AS table_name, 'source_id' AS column_name
  UNION ALL
  SELECT 'swe_workflow_bindings' AS table_name, 'skill_id' AS column_name
  UNION ALL
  SELECT 'swe_workflow_bindings' AS table_name, 'current_version' AS column_name
  UNION ALL
  SELECT 'swe_workflow_bindings' AS table_name, 'enabled' AS column_name
  UNION ALL
  SELECT 'swe_workflow_binding_versions' AS table_name, 'binding_id' AS column_name
  UNION ALL
  SELECT 'swe_workflow_binding_versions' AS table_name, 'version' AS column_name
  UNION ALL
  SELECT 'swe_workflow_binding_versions' AS table_name, 'skill_id' AS column_name
  UNION ALL
  SELECT 'swe_workflow_binding_versions' AS table_name, 'config_json' AS column_name
) required
LEFT JOIN information_schema.columns actual
  ON actual.table_schema=DATABASE()
 AND actual.table_name=required.table_name
 AND actual.column_name=required.column_name
WHERE actual.column_name IS NULL
ORDER BY required.table_name, required.column_name;

-- 第二项：人工核对类型、默认值、精度、字符集和中文注释；不自动变更已有列。
SELECT table_name,column_name,column_type,is_nullable,column_default,
       datetime_precision,character_set_name,collation_name,extra,column_comment
FROM information_schema.columns
WHERE table_schema=DATABASE()
  AND table_name IN (
  'swe_cron_jobs_v2',
  'swe_cron_job_states',
  'swe_cron_registration_journal',
  'swe_cron_execution_runs',
  'swe_cron_managed_broadcast_tasks',
  'swe_cron_managed_broadcast_items',
  'swe_cron_managed_dispatch_controls',
  'swe_cron_managed_mode_items',
  'swe_cron_managed_trigger_receipts',
  'swe_cron_jobs',
  'swe_cron_executions',
  'swe_cron_dispatch_scope_leases',
  'swe_cron_dispatch_worker_capacity',
  'swe_cron_dispatch_intents',
  'swe_cron_dispatch_model_worker_policy',
  'swe_cron_dispatch_worker_strategy',
  'swe_workflow_bindings',
  'swe_workflow_binding_versions'
  )
ORDER BY table_name,ordinal_position;

-- 第三项：人工核对唯一键与恢复索引，自增字段必须是索引键。
SELECT table_name,index_name,non_unique,seq_in_index,column_name
FROM information_schema.statistics
WHERE table_schema=DATABASE()
  AND table_name IN (
  'swe_cron_jobs_v2',
  'swe_cron_job_states',
  'swe_cron_registration_journal',
  'swe_cron_execution_runs',
  'swe_cron_managed_broadcast_tasks',
  'swe_cron_managed_broadcast_items',
  'swe_cron_managed_dispatch_controls',
  'swe_cron_managed_mode_items',
  'swe_cron_managed_trigger_receipts',
  'swe_cron_jobs',
  'swe_cron_executions',
  'swe_cron_dispatch_scope_leases',
  'swe_cron_dispatch_worker_capacity',
  'swe_cron_dispatch_intents',
  'swe_cron_dispatch_model_worker_policy',
  'swe_cron_dispatch_worker_strategy',
  'swe_workflow_bindings',
  'swe_workflow_binding_versions'
  )
ORDER BY table_name,index_name,seq_in_index;

-- 第四项：事务引擎和排序规则；新表预期 InnoDB，身份需精确区分大小写。
SELECT table_name,engine,table_collation,table_comment
FROM information_schema.tables
WHERE table_schema=DATABASE()
  AND table_name IN (
  'swe_cron_jobs_v2',
  'swe_cron_job_states',
  'swe_cron_registration_journal',
  'swe_cron_execution_runs',
  'swe_cron_managed_broadcast_tasks',
  'swe_cron_managed_broadcast_items',
  'swe_cron_managed_dispatch_controls',
  'swe_cron_managed_mode_items',
  'swe_cron_managed_trigger_receipts',
  'swe_cron_jobs',
  'swe_cron_executions',
  'swe_cron_dispatch_scope_leases',
  'swe_cron_dispatch_worker_capacity',
  'swe_cron_dispatch_intents',
  'swe_cron_dispatch_model_worker_policy',
  'swe_cron_dispatch_worker_strategy',
  'swe_workflow_bindings',
  'swe_workflow_binding_versions'
  )
ORDER BY table_name;
