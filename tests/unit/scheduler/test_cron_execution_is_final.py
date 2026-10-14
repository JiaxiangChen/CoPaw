"""Exercise final markers on persisted rows, without proving MySQL locks."""

import asyncio
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest

from monitor.app.models.cron import ExecutionSyncRequest as MonitorRequest
from monitor.app.services.cron import sync_service as monitor_sync
from scheduler.app.models.cron import ExecutionSyncRequest
from scheduler.app.services.cron import execution_sync_service as module


class Cursor:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params=()):
        self.db.statements.append(sql)
        if self.db.fail_insert and "INSERT INTO swe_cron_executions" in sql:
            raise RuntimeError("insert failed")
        self.result = self.db.connection.execute(
            sql.replace("%s", "?").replace("FOR UPDATE", ""),
            tuple(
                p.isoformat() if isinstance(p, datetime) else p for p in params
            ),
        )
        self.lastrowid = self.result.lastrowid
        self.rowcount = self.result.rowcount

    async def fetchone(self):
        return self.result.fetchone()

    async def fetchall(self):
        return self.result.fetchall()


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.statements = []
        self.fail_insert = False
        self.commit_error = False
        self.connection.executescript("""
            CREATE TABLE swe_cron_dispatch_intents (
                id INTEGER PRIMARY KEY, batch_id TEXT, job_id TEXT,
                tenant_id TEXT, source_id TEXT, attempt_count INTEGER
            );
            INSERT INTO swe_cron_dispatch_intents
            VALUES (7,'batch-1','job-1','tenant-1','source-1',1);
            CREATE TABLE swe_cron_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT, job_name TEXT, tenant_id TEXT,
                scheduled_time TEXT, actual_time TEXT, end_time TEXT,
                duration_ms INTEGER, status TEXT, error_message TEXT,
                instance_id TEXT, executor_leader TEXT, is_manual INTEGER,
                trace_id TEXT, session_id TEXT, input_snapshot TEXT,
                output_preview TEXT, meta TEXT,
                dispatch_intent_id INTEGER, dispatch_batch_id TEXT,
                dispatch_attempt INTEGER,
                notification_status TEXT, notification_due_at TEXT,
                notification_timezone TEXT, is_read INTEGER, read_at TEXT,
                created_at TEXT, is_final INTEGER NOT NULL DEFAULT 1
            );
        """)

    @asynccontextmanager
    async def acquire(self):
        yield self

    def cursor(self):
        return Cursor(self)

    async def begin(self):
        self.connection.execute("BEGIN")

    async def commit(self):
        self.connection.commit()
        if self.commit_error:
            raise RuntimeError("commit response lost")

    async def rollback(self):
        self.connection.rollback()

    def attempt(self, number):
        self.connection.execute(
            "UPDATE swe_cron_dispatch_intents SET attempt_count=? WHERE id=7",
            (number,),
        )

    def finals(self):
        return self.connection.execute(
            "SELECT is_final FROM swe_cron_executions ORDER BY id",
        ).fetchall()


@pytest.fixture
def db(monkeypatch):
    value = Database()
    monkeypatch.setattr(module, "get_db_connection", lambda: value)
    yield value
    value.connection.close()


def request(attempt=1, **kwargs):
    return ExecutionSyncRequest(
        job_id="job-1",
        tenant_id="tenant-1",
        source_id="source-1",
        actual_time=datetime(2026, 10, 14, 10),
        status="error",
        meta=json.dumps(
            {
                "cron_dispatch": {
                    "intent_id": 7,
                    "batch_id": "batch-1",
                    "dispatch_attempt": attempt,
                }
            }
        ),
        **kwargs,
    )


def test_create_and_upgrade_default_all_records_to_final():
    sql = (
        Path(__file__).resolve().parents[3]
        / "scripts/sql/cron_execution_is_final.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN is_final TINYINT(1) NOT NULL DEFAULT 1" in sql
    # Only remove MySQL-specific annotations to exercise defaults in SQLite.
    statement = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    statement = re.sub(r"\s+COMMENT '[^']*'", "", statement)
    statement = statement.replace("AFTER dispatch_attempt", "")
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(
            "CREATE TABLE swe_cron_executions(id INTEGER); "
            "INSERT INTO swe_cron_executions VALUES(1),(2)"
        )
        db.executescript(statement)
        assert db.execute(
            "SELECT is_final FROM swe_cron_executions"
        ).fetchall() == [(1,), (1,)]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_ordinary_records_all_remain_final(db):
    service = module.ExecutionSyncService()
    ordinary = request().model_copy(update={"meta": ""})
    await service.record_execution(ordinary)
    await service.record_execution(ordinary)
    assert db.finals() == [(1,), (1,)]


@pytest.mark.asyncio
async def test_monitor_ordinary_records_use_database_default(db, monkeypatch):
    monkeypatch.setattr(monitor_sync, "get_db_connection", lambda: db)
    ordinary = MonitorRequest(
        job_id="job-1",
        tenant_id="tenant-1",
        actual_time=datetime(2026, 10, 14, 10),
        status="success",
    )
    service = monitor_sync.SyncService()
    await service.record_execution(ordinary)
    await service.record_execution(ordinary)
    assert db.finals() == [(1,), (1,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_manual", [False, True])
async def test_new_attempt_replaces_previous_result_only_on_insert(
    db, is_manual
):
    service = module.ExecutionSyncService()
    first = await service.record_dispatch_execution(request())
    db.attempt(2)
    assert db.finals() == [(1,)]
    second = await service.record_dispatch_execution(
        request(2, is_manual=is_manual)
    )
    assert second != first
    assert db.finals() == [(0,), (1,)]
    assert any("FOR UPDATE" in sql for sql in db.statements)


@pytest.mark.asyncio
async def test_duplicate_receipt_does_not_insert_or_change_markers(db):
    service = module.ExecutionSyncService()
    first = await service.record_dispatch_execution(request())
    again = await service.record_dispatch_execution(request())
    assert first == again
    assert db.finals() == [(1,)]


@pytest.mark.asyncio
async def test_legacy_final_rows_only_replace_latest(db):
    service = module.ExecutionSyncService()
    for attempt in range(1, 4):
        await service.record_execution(request(attempt))
    db.attempt(4)
    await service.record_dispatch_execution(request(4))
    assert db.finals() == [(1,), (1,), (0,), (1,)]


@pytest.mark.asyncio
async def test_insert_failure_rolls_back_previous_marker(db):
    service = module.ExecutionSyncService()
    await service.record_dispatch_execution(request())
    db.attempt(2)
    db.fail_insert = True
    with pytest.raises(RuntimeError, match="insert failed"):
        await service.record_dispatch_execution(request(2))
    assert db.finals() == [(1,)]
    assert not db.connection.in_transaction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "wrong"),
        ("job_id", "wrong"),
        ("source_id", "wrong"),
    ],
)
async def test_wrong_identity_cannot_replace_result(db, field, value):
    service = module.ExecutionSyncService()
    await service.record_dispatch_execution(request())
    db.attempt(2)
    wrong = request(2).model_copy(update={field: value})
    with pytest.raises(RuntimeError):
        await service.record_dispatch_execution(wrong)
    assert db.finals() == [(1,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [1, 3])
async def test_old_or_future_attempt_cannot_replace_result(db, attempt):
    service = module.ExecutionSyncService()
    await service.record_dispatch_execution(request())
    db.attempt(2)
    with pytest.raises(RuntimeError):
        await service.record_dispatch_execution(request(attempt))
    assert db.finals() == [(1,)]


@pytest.mark.asyncio
async def test_other_batch_and_ordinary_records_are_untouched(db):
    service = module.ExecutionSyncService()
    await service.record_execution(request().model_copy(update={"meta": ""}))
    other = request()
    meta = json.loads(other.meta)
    meta["cron_dispatch"]["batch_id"] = "other-batch"
    await service.record_execution(
        other.model_copy(update={"meta": json.dumps(meta)})
    )
    await service.record_dispatch_execution(request())
    db.attempt(2)
    await service.record_dispatch_execution(request(2))
    assert db.finals() == [(1,), (1,), (0,), (1,)]


@pytest.mark.asyncio
async def test_wrong_tenant_legacy_row_is_not_replaced(db):
    service = module.ExecutionSyncService()
    wrong = request().model_copy(update={"tenant_id": "other-tenant"})
    await service.record_execution(wrong)
    db.attempt(2)
    await service.record_dispatch_execution(request(2))
    assert db.finals() == [(1,), (1,)]


@pytest.mark.asyncio
async def test_committed_result_is_reused_after_commit_response_loss(db):
    service = module.ExecutionSyncService()
    db.commit_error = True
    with pytest.raises(RuntimeError, match="commit response lost"):
        await service.record_dispatch_execution(request())
    db.commit_error = False
    execution_id = await service.record_dispatch_execution(request())
    assert execution_id == 1
    assert db.finals() == [(1,)]


@pytest.mark.asyncio
async def test_cancelled_insert_rolls_back_previous_marker(db, monkeypatch):
    service = module.ExecutionSyncService()
    await service.record_dispatch_execution(request())
    db.attempt(2)

    async def cancelled(*_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(service, "_insert_execution", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await service.record_dispatch_execution(request(2))
    assert db.finals() == [(1,)]
    assert not db.connection.in_transaction


@pytest.mark.asyncio
async def test_missing_attempt_cannot_bypass_current_attempt_check(db):
    service = module.ExecutionSyncService()
    await service.record_dispatch_execution(request())
    db.attempt(2)
    meta = json.loads(request().meta)
    del meta["cron_dispatch"]["dispatch_attempt"]
    with pytest.raises(RuntimeError, match="attempt mismatch"):
        await service.record_dispatch_execution(
            request().model_copy(update={"meta": json.dumps(meta)}),
        )
    assert db.finals() == [(1,)]


@pytest.mark.asyncio
async def test_higher_persisted_attempt_cannot_be_replaced(db):
    service = module.ExecutionSyncService()
    await service.record_execution(request(3))
    db.attempt(2)
    with pytest.raises(RuntimeError, match="newer dispatch execution"):
        await service.record_dispatch_execution(request(2))
    assert db.finals() == [(1,)]
