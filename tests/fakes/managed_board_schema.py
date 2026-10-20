# -*- coding: utf-8 -*-
"""新批看板的SQLite测试结构，不属于应用启动DDL。"""

import json


def add_board_tables(connection):
    connection.create_function(
        "CONCAT",
        -1,
        lambda *parts: (
            None
            if any(part is None for part in parts)
            else "".join(map(str, parts))
        ),
    )
    connection.create_function(
        "JSON_LENGTH",
        1,
        lambda value: len(json.loads(value)),
    )
    connection.executescript("""
        CREATE TABLE swe_cron_managed_batch_members (
            batch_id TEXT NOT NULL, member_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL, source_id TEXT NOT NULL,
            agent_id TEXT NOT NULL, job_id TEXT NOT NULL,
            initial_execution_id TEXT NOT NULL, current_execution_id TEXT NOT NULL,
            business_attempt INTEGER NOT NULL, member_role TEXT NOT NULL,
            dispatch_order INTEGER NOT NULL, version INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (batch_id, member_id),
            UNIQUE (batch_id, tenant_id, source_id, agent_id, job_id)
        );
        CREATE TABLE swe_cron_managed_dispatch_events (
            event_order INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE, event_key TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL, source_id TEXT NOT NULL,
            agent_id TEXT NOT NULL, job_id TEXT NOT NULL,
            execution_id TEXT, batch_id TEXT, member_id TEXT,
            event_type TEXT NOT NULL, execution_version INTEGER,
            actor_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
    """)
