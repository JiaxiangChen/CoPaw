# -*- coding: utf-8 -*-
"""真实 SQLite 查询夹具；不替换追踪查询和映射逻辑。"""

import json

from tests.unit.scheduler.test_managed_cron_queries import QueryDatabase


class TrackingDatabase(QueryDatabase):
    def __init__(self):
        super().__init__()
        self.conn.create_function("JSON_UNQUOTE", 1, lambda value: value)
        self.conn.create_function(
            "CONCAT",
            -1,
            lambda *values: "".join(str(value) for value in values),
        )
        self.conn.executescript("""
            ALTER TABLE swe_cron_jobs_v2 ADD COLUMN original_cron_expr TEXT;
            ALTER TABLE swe_cron_jobs_v2 ADD COLUMN job_origin TEXT;
            ALTER TABLE swe_cron_jobs_v2 ADD COLUMN plan_id TEXT;
            ALTER TABLE swe_cron_jobs_v2 ADD COLUMN created_at TEXT;
            ALTER TABLE swe_cron_jobs_v2 ADD COLUMN updated_at TEXT;
            ALTER TABLE swe_cron_job_states ADD COLUMN updated_at TEXT;
        """)
        identity = "tenant_id TEXT,source_id TEXT,agent_id TEXT,job_id TEXT"
        tables = {
            "swe_cron_execution_runs": (
                "execution_id TEXT PRIMARY KEY,queue_order INTEGER,"
                "actor_id TEXT,kind TEXT,definition_version INTEGER,"
                "spec_json TEXT,dispatch_meta_json TEXT,status TEXT,"
                "dispatch_attempt INTEGER,accepted_at TEXT,started_at TEXT,"
                "finished_at TEXT,settled_status TEXT,settled_at TEXT,"
                "settled_error_code TEXT,history_execution_id INTEGER,"
                "error_code TEXT,result_json TEXT,output_preview TEXT,trace_id TEXT,"
                "provider_id TEXT,model_id TEXT,scheduled_fire_at TEXT,"
                "not_before_at TEXT,created_at TEXT,updated_at TEXT"
            ),
            "swe_cron_registration_journal": (
                "operation_id TEXT PRIMARY KEY,actor_id TEXT,action TEXT,"
                "request_id TEXT,expected_version INTEGER,payload_json TEXT,"
                "progress_json TEXT,status TEXT,stage TEXT,error_code TEXT,"
                "version INTEGER,created_at TEXT,updated_at TEXT"
            ),
            "swe_cron_managed_dispatch_events": (
                "event_order INTEGER PRIMARY KEY,event_id TEXT,event_key TEXT,"
                "execution_id TEXT,batch_id TEXT,member_id TEXT,event_type TEXT,"
                "execution_version INTEGER,actor_id TEXT,occurred_at TEXT,details_json TEXT"
            ),
            "swe_cron_managed_trigger_receipts": (
                "receipt_id TEXT PRIMARY KEY,external_job_id TEXT,operation_id TEXT,"
                "kind TEXT,trigger_id TEXT,logical_fire_at TEXT,physical_fire_at TEXT,"
                "batch_id TEXT,status TEXT,response_json TEXT,members_json TEXT,created_at TEXT"
            ),
            "swe_cron_managed_batch_members": (
                "batch_id TEXT,member_id TEXT,initial_execution_id TEXT,"
                "current_execution_id TEXT,business_attempt INTEGER,member_role TEXT,"
                "created_at TEXT,updated_at TEXT"
            ),
            "swe_cron_managed_broadcast_tasks": (
                "task_id TEXT PRIMARY KEY,actor_id TEXT,source_definition_version INTEGER,"
                "payload_json TEXT,status TEXT,sync_status TEXT,error_code TEXT,"
                "created_at TEXT,updated_at TEXT"
            ),
            "swe_cron_managed_broadcast_items": (
                "task_id TEXT,index_no INTEGER,operation TEXT,"
                "expected_definition_version INTEGER,status TEXT,plan_json TEXT,"
                "result_json TEXT,warning TEXT,created_at TEXT,updated_at TEXT"
            ),
            "swe_cron_managed_mode_items": (
                "operation_id TEXT,index_no INTEGER,sync_operation_id TEXT,"
                "status TEXT,error_code TEXT"
            ),
        }
        for table, columns in tables.items():
            self.conn.execute(f"CREATE TABLE {table} ({identity},{columns})")
        self.conn.executescript("""
            CREATE TABLE swe_cron_failure_retry_calls (
                call_id TEXT PRIMARY KEY,source_id TEXT,business_date TEXT,
                window_start TEXT,cutoff_at TEXT,status TEXT,error_code TEXT,
                created_at TEXT,started_at TEXT,finished_at TEXT
            );
            CREATE TABLE swe_cron_executions (
                id INTEGER PRIMARY KEY,job_id TEXT,tenant_id TEXT,job_name TEXT,
                actual_time TEXT,end_time TEXT,scheduled_time TEXT,duration_ms INTEGER,
                status TEXT,async_status TEXT,error_message TEXT,output_preview TEXT,
                trace_id TEXT,session_id TEXT,meta TEXT,is_final INTEGER,
                need_notification INTEGER,notification_status TEXT,notification_due_at TEXT,
                notification_timezone TEXT,notification_sent_at TEXT,
                notification_attempts INTEGER,notification_error TEXT,is_read INTEGER,
                read_at TEXT,created_at TEXT
            );
        """)
        self.reads = []

    async def fetch_all(self, query, params=None):
        self.reads.append((query, params))
        return await super().fetch_all(query, params)

    def insert(self, table, **values):
        self._insert(table, values)

    def run(
        self,
        execution="run-1",
        *,
        tenant="alice",
        source="bank",
        **values,
    ):
        row = {
            "execution_id": execution,
            "queue_order": 1,
            "tenant_id": tenant,
            "source_id": source,
            "agent_id": "default",
            "job_id": "job-1",
            "actor_id": tenant,
            "kind": "ordinary",
            "status": "success",
            "definition_version": 1,
            "dispatch_meta_json": json.dumps(
                {"protocol": "scheduler_managed_v1"},
            ),
            "created_at": "2026-10-27 00:00:00",
            **values,
        }
        self.insert("swe_cron_execution_runs", **row)
        return row
