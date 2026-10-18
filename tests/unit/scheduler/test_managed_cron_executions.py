# -*- coding: utf-8 -*-
"""通过真实事务和 SQL 验证新执行身份与管理归属。"""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from tests.unit.scheduler.test_managed_cron_management import (
    ManagementDatabase,
    PlatformClient,
    ResourceClient,
    create_payload,
)


class ExecutionDatabase(ManagementDatabase):
    def __init__(self):
        from tests.fakes.managed_board_schema import add_board_tables

        super().__init__()
        self.conn.create_function("JSON_UNQUOTE", 1, lambda value: value)
        self.conn.executescript("""
            DROP TABLE IF EXISTS swe_cron_execution_runs;
            CREATE TABLE swe_cron_execution_runs (
                queue_order INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT UNIQUE, execution_key TEXT UNIQUE,
                dispatch_order INTEGER NOT NULL DEFAULT 0,
                tenant_id TEXT, source_id TEXT, agent_id TEXT, job_id TEXT,
                scope_id TEXT, actor_id TEXT, definition_version INTEGER,
                provider_id TEXT, model_id TEXT,
                enqueued_state_version INTEGER, started_order INTEGER,
                kind TEXT, trigger_key TEXT, scheduled_fire_at TEXT,
                not_before_at TEXT,
                spec_json TEXT, dispatch_meta_json TEXT, status TEXT,
                dispatch_attempt INTEGER DEFAULT 0,
                dispatch_owner TEXT, dispatch_lease_until TEXT,
                run_owner TEXT, run_lease_until TEXT, accepted_at TEXT,
                started_at TEXT, finished_at TEXT, history_execution_id INTEGER,
                output_preview TEXT, trace_id TEXT, error_code TEXT,
                result_json TEXT, version INTEGER DEFAULT 1,
                settled_status TEXT, settled_at TEXT, settled_error_code TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE swe_cron_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT, job_name TEXT, tenant_id TEXT,
                scheduled_time TEXT, actual_time TEXT, end_time TEXT,
                duration_ms INTEGER, status TEXT, async_status TEXT, error_message TEXT,
                instance_id TEXT, executor_leader TEXT, is_manual INTEGER,
                trace_id TEXT, session_id TEXT, input_snapshot TEXT,
                output_preview TEXT, meta TEXT, notification_status TEXT,
                notification_due_at TEXT, notification_timezone TEXT,
                is_read INTEGER, read_at TEXT, created_at TEXT
            );
        """)
        add_board_tables(self.conn)

    async def fetch_one(self, query, params=None):
        if query == "SELECT LAST_INSERT_ID() AS id":
            return dict(
                self.conn.execute(
                    "SELECT last_insert_rowid() AS id",
                ).fetchone(),
            )
        return await super().fetch_one(query, params)


@pytest.fixture
def execution_setup():
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )

    db = ExecutionDatabase()
    caller = CallerIdentity("alice", "bank", "default", "alice")
    management = ManagementService(db, ResourceClient(), PlatformClient())
    yield db, caller, management
    db.conn.close()


async def enqueue(execution_setup, *, kind="ordinary", trigger="period-1"):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )

    db, caller, management = execution_setup
    created = await management.create(caller, create_payload())
    command = ExecutionRequest(
        kind=kind,
        trigger_key=trigger,
        scheduled_at=datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    row, reused = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        command,
    )
    return row, reused


@pytest.mark.asyncio
async def test_ordinary_and_batch_same_period_share_one_frozen_execution(
    execution_setup,
):
    first, reused = await enqueue(execution_setup)
    second, reused_again = await enqueue(execution_setup, kind="batch")
    assert not reused
    assert reused_again
    assert first["execution_id"] == second["execution_id"]
    assert first["status"] == "queued"
    spec = json.loads(first["spec_json"])
    assert spec["meta"]["task_chat_id"].startswith("chat-")
    assert first["definition_version"] == 1


@pytest.mark.asyncio
async def test_enqueue_rejects_legacy_or_foreign_task(execution_setup):
    from scheduler.app.services.cron.managed.contracts import (
        ManagedCronNotFound,
    )
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )

    db, caller, _ = execution_setup
    db.add("legacy", owner="legacy")
    command = ExecutionRequest("manual", "request-1")
    with pytest.raises(ManagedCronNotFound):
        await ExecutionStore(db).enqueue(caller, "legacy", command)
    db.add("foreign", actor="bob")
    with pytest.raises(ManagedCronNotFound):
        await ExecutionStore(db).enqueue(caller, "foreign", command)


@pytest.mark.asyncio
async def test_enqueue_blocks_pending_update_without_inventing_later_run(
    execution_setup,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionConflict,
        ExecutionRequest,
        ExecutionStore,
    )

    db, caller, management = execution_setup
    created = await management.create(caller, create_payload())
    db.conn.execute(
        "UPDATE swe_cron_job_states SET management_status='updating'",
    )
    with pytest.raises(ExecutionConflict, match="not_ready"):
        await ExecutionStore(db).enqueue(
            caller,
            created["job_id"],
            ExecutionRequest("ordinary", "new-period"),
        )
    assert not db.conn.execute(
        "SELECT * FROM swe_cron_execution_runs",
    ).fetchall()


@pytest.mark.asyncio
async def test_enqueued_snapshot_is_not_replaced_by_later_definition(
    execution_setup,
):
    from scheduler.app.services.cron.managed.executions import ExecutionStore

    db, caller, management = execution_setup
    first, _ = await enqueue(execution_setup)
    await management.update(
        caller,
        first["job_id"],
        {
            "request_id": "update-after-enqueue",
            "expected_definition_version": 1,
            "spec": create_payload(name="new definition")["spec"],
        },
    )
    stored = await ExecutionStore(db).get(
        caller,
        first["job_id"],
        first["execution_id"],
    )
    assert json.loads(stored["spec_json"])["name"] == "report"
    assert stored["definition_version"] == 1


@pytest.mark.asyncio
async def test_same_manual_request_reuses_identity_but_not_scheduled_period(
    execution_setup,
):
    manual, _ = await enqueue(execution_setup, kind="manual")
    duplicate, reused = await enqueue(execution_setup, kind="manual")
    scheduled, _ = await enqueue(execution_setup)
    assert reused
    assert manual["execution_id"] == duplicate["execution_id"]
    assert manual["execution_id"] != scheduled["execution_id"]


@pytest.mark.asyncio
async def test_manual_retry_cannot_change_its_expected_definition_version(
    execution_setup,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionConflict,
        ExecutionRequest,
        ExecutionStore,
    )

    db, caller, management = execution_setup
    manual, _ = await enqueue(execution_setup, kind="manual")
    await management.update(
        caller,
        manual["job_id"],
        {
            "request_id": "content-update",
            "expected_definition_version": 1,
            "spec": create_payload(name="changed")["spec"],
        },
    )
    with pytest.raises(ExecutionConflict, match="request_content"):
        await ExecutionStore(db).enqueue(
            caller,
            manual["job_id"],
            ExecutionRequest(
                "manual",
                "period-1",
                expected_definition_version=2,
            ),
        )


@pytest.mark.asyncio
async def test_concurrent_enqueue_inserts_one_row(execution_setup):
    # 测试适配器串行执行真实事务，覆盖唯一键及重复读取路径。
    # 不通过伪造查询结果绕过生产 SQL 的去重规则。
    _, caller, management = execution_setup
    await management.create(caller, create_payload())
    first, second = await asyncio.gather(
        enqueue(execution_setup),
        enqueue(execution_setup),
    )
    assert first[0]["execution_id"] == second[0]["execution_id"]
    db = execution_setup[0]
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 1
    )
