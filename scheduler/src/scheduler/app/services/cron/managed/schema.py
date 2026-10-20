# -*- coding: utf-8 -*-
"""启动前只读校验实际使用的字段，不创建或更新业务表。"""

REQUIRED_COLUMNS = {
    "swe_cron_jobs_v2": (
        "tenant_id source_id agent_id job_id scope_id name task_type "
        "tenant_name bbk_id skill_ids workflow_binding_id "
        "enabled cron_expr timezone spec_json offset_minutes "
        "offset_window_hours enable_offset management_owner "
        "creator_user_id definition_version deleted_at parent_tenant_id "
        "parent_source_id parent_agent_id parent_job_id original_cron_expr "
        "job_origin plan_id source_definition_version created_at "
        "updated_at"
    ),
    "swe_cron_job_states": (
        "tenant_id source_id agent_id job_id task_chat_id task_session_id "
        "ordinary_external_job_id batch_external_job_id dispatch_state "
        "pause_reason auto_paused_at next_run_at next_run_times_json "
        "last_status last_run_at last_prefetch_at last_error "
        "has_scheduled_result latest_scheduled_preview "
        "last_scheduled_run_at unread_execution_count management_status "
        "management_version state_version queue_invalidation_version "
        "state_source management_updated_at created_at updated_at"
    ),
    "swe_cron_registration_journal": (
        "operation_id active_key tenant_id source_id agent_id job_id "
        "actor_id action request_id request_hash expected_version "
        "payload_json progress_json status stage error_code lease_owner "
        "lease_until version created_at updated_at"
    ),
    "swe_cron_execution_runs": (
        "execution_id execution_key queue_order dispatch_order "
        "tenant_id source_id agent_id job_id "
        "scope_id actor_id definition_version provider_id model_id "
        "enqueued_state_version started_order kind trigger_key "
        "scheduled_fire_at not_before_at spec_json dispatch_meta_json "
        "status dispatch_attempt dispatch_owner dispatch_lease_until "
        "run_owner run_lease_until accepted_at started_at finished_at "
        "history_execution_id output_preview trace_id error_code "
        "result_json version created_at updated_at "
        "settled_status settled_at settled_error_code"
    ),
    "swe_cron_managed_broadcast_tasks": (
        "created_order task_id active_key tenant_id source_id agent_id "
        "job_id actor_id actor_is_manager request_id request_hash "
        "source_definition_version payload_json tenant_count status "
        "sync_status error_code lease_owner lease_until version created_at "
        "updated_at"
    ),
    "swe_cron_managed_broadcast_items": (
        "task_id index_no tenant_id source_id agent_id job_id "
        "expected_definition_version operation status plan_json "
        "result_json cron_expr timezone offset_minutes warning lease_owner "
        "lease_until version created_at updated_at"
    ),
    "swe_cron_managed_dispatch_controls": (
        "tenant_id source_id agent_id job_id state version operation_id "
        "sync_status error_code"
    ),
    "swe_cron_managed_mode_items": (
        "operation_id index_no tenant_id source_id agent_id job_id "
        "expected_definition_version sync_operation_id status error_code"
    ),
    "swe_cron_managed_trigger_receipts": (
        "receipt_id callback_key tenant_id source_id agent_id job_id "
        "external_job_id operation_id kind trigger_id logical_fire_at "
        "physical_fire_at batch_id status response_json members_json "
        "created_at"
    ),
    "swe_cron_jobs": (
        "id name tenant_id source_id tenant_name bbk_id enabled task_type "
        "cron_expr timezone channel target_user_id target_session_id "
        "timeout_seconds max_concurrency misfire_grace_seconds "
        "text_content request_input creator_user_id task_chat_id "
        "task_session_id plan_id meta skill_ids workflow_binding_id "
        "job_origin status pause_reason deleted_at created_at updated_at"
    ),
    "swe_cron_executions": (
        "id job_id job_name tenant_id scheduled_time actual_time end_time "
        "duration_ms status async_status error_message instance_id executor_leader "
        "is_manual trace_id session_id input_snapshot output_preview meta "
        "notification_status notification_due_at notification_timezone "
        "is_read read_at created_at"
    ),
    "swe_cron_dispatch_scope_leases": (
        "source_id provider_id model_id lock_owner locked_at "
        "lease_expires_at heartbeat_at"
    ),
    "swe_cron_dispatch_worker_capacity": (
        "id worker_id source_id provider_id model_id strategy_id "
        "previous_workers baseline_workers min_workers max_workers "
        "effective_workers pending_count claimed_count running_count "
        "success_count failure_count error_rate matched_rule avg_latency_ms "
        "decision_reason created_at"
    ),
    "swe_cron_dispatch_intents": "id source_id provider_id model_id status",
    "swe_cron_dispatch_model_worker_policy": (
        "source_id provider_id model_id default_strategy_id "
        "strategy_schedule enabled"
    ),
    "swe_cron_dispatch_worker_strategy": (
        "strategy_id min_workers baseline_workers max_workers "
        "adjust_interval_seconds feedback_window_seconds "
        "stale_execution_seconds error_rate_rules enabled"
    ),
}


class ManagedSchemaUnavailable(RuntimeError):
    def __init__(self, table):
        self.table = table
        super().__init__("cron_managed_schema_unavailable")


async def assert_managed_schema_ready(db):
    """LIMIT 0 校验表和列可读，不扫描任务数据，也不隐式修复数据库。"""
    for table, fields in REQUIRED_COLUMNS.items():
        columns = ",".join(f"`{name}`" for name in fields.split())
        try:
            await db.fetch_all(f"SELECT {columns} FROM `{table}` LIMIT 0")
        except Exception as exc:
            raise ManagedSchemaUnavailable(table) from exc
