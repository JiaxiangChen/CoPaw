# -*- coding: utf-8 -*-
"""Exercise reconciliation against persisted execution rows and current attempts."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from scheduler.app.services.cron import (
    dispatch_intent_service as intent_module,
)
from scheduler.app.services.cron.dispatch_intent_service import (
    CronDispatchIntentService,
)
from scheduler.app.services.cron.scheduling_service import (
    CronSchedulingService,
)
from scheduler.app.models.cron import ExecutionSyncRequest
from scheduler.app.routers import cron as cron_router
from fastapi import HTTPException

NOW = datetime(2026, 9, 4, 12, 0)


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, sql, params=()):
        cursor = self.db.run(sql, params)
        self.rowcount = cursor.rowcount
        self.rows = [dict(row) for row in cursor.fetchall()]

    async def fetchall(self):
        return self.rows


class _SqliteDb:
    """Run the service's SELECT/UPDATE SQL; omit MySQL locking syntax only."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.before_update = None
        self.connection.executescript(
            """
            CREATE TABLE swe_cron_dispatch_intents (
                id INTEGER PRIMARY KEY, batch_id TEXT, job_id TEXT,
                tenant_id TEXT, source_id TEXT, provider_id TEXT, model_id TEXT,
                status TEXT, attempt_count INTEGER, max_attempts INTEGER,
                due_at TEXT, locked_at TEXT, lock_owner TEXT,
                completed_at TEXT, updated_at TEXT, error_message TEXT,
                dispatch_order INTEGER
            );
            CREATE TABLE swe_cron_executions (
                id INTEGER PRIMARY KEY, job_id TEXT, tenant_id TEXT,
                trace_id TEXT, status TEXT, async_status TEXT, error_message TEXT,
                end_time TEXT, dispatch_intent_id INTEGER,
                dispatch_batch_id TEXT, dispatch_attempt INTEGER
            );
            CREATE TABLE swe_cron_dispatch_events (
                id INTEGER PRIMARY KEY, intent_id INTEGER, event_type TEXT,
                created_at TEXT
            );
            """,
        )

    def run(self, sql, params=()):
        sql = sql.replace("FOR UPDATE SKIP LOCKED", "").replace(
            "FOR UPDATE",
            "",
        )
        if self.before_update and sql.lstrip().startswith("UPDATE"):
            callback, self.before_update = self.before_update, None
            callback()
        values = tuple(
            value.isoformat(" ") if isinstance(value, datetime) else value
            for value in (params or ())
        )
        return self.connection.execute(sql.replace("%s", "?"), values)

    async def fetch_all(self, sql, params=()):
        return [dict(row) for row in self.run(sql, params).fetchall()]

    async def fetch_one(self, sql, params=()):
        row = self.run(sql, params).fetchone()
        return dict(row) if row else None

    async def execute(self, sql, params=()):
        return self.run(sql, params).rowcount

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def begin(self):
        self.connection.execute("BEGIN")

    async def commit(self):
        self.connection.commit()

    async def rollback(self):
        self.connection.rollback()

    def cursor(self):
        return _Cursor(self)

    def insert(self, table, **row):
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        self.run(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            row.values(),
        )

    def add_intent(self, **overrides):
        self.insert(
            "swe_cron_dispatch_intents",
            **{
                "id": 7,
                "batch_id": "batch-1",
                "job_id": "job-1",
                "tenant_id": "tenant-1",
                "source_id": "source-a",
                "provider_id": "default",
                "model_id": "default",
                "status": "dispatched",
                "attempt_count": 1,
                "max_attempts": 3,
                "due_at": NOW - timedelta(hours=3),
                "locked_at": NOW - timedelta(hours=3),
                "lock_owner": "worker-1",
                "updated_at": NOW - timedelta(hours=3),
                "error_message": "",
                "dispatch_order": 1,
                **overrides,
            },
        )

    def add_execution(self, **overrides):
        self.insert(
            "swe_cron_executions",
            **{
                "id": 42,
                "job_id": "job-1",
                "tenant_id": "tenant-1",
                "trace_id": "trace-1",
                "status": "success",
                "async_status": None,
                "error_message": "",
                "end_time": NOW - timedelta(hours=1),
                "dispatch_intent_id": 7,
                "dispatch_batch_id": "batch-1",
                "dispatch_attempt": 1,
                **overrides,
            },
        )

    def intent(self):
        return dict(
            self.connection.execute(
                "SELECT * FROM swe_cron_dispatch_intents WHERE id=7",
            ).fetchone(),
        )


@pytest.fixture
def persisted_store(monkeypatch):
    db = _SqliteDb()
    monkeypatch.setattr(intent_module, "get_db_connection", lambda: db)
    store = CronDispatchIntentService()
    store._record_event_best_effort = AsyncMock()
    store._refresh_batch_counts_for_rows = AsyncMock()
    yield db, store
    db.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_status", "async_status", "expected", "error"),
    [
        ("success", "success", "completed", ""),
        ("success", "error", "pending", "子任务执行失败"),
        ("error", None, "pending", "agent failed"),
        ("error", "success", "pending", "agent failed"),
        ("cancelled", None, "pending", "agent failed"),
        ("timeout", None, "pending", "agent failed"),
        ("success", None, "dispatched", ""),
        ("success", "", "dispatched", ""),
        ("success", "pending", "dispatched", ""),
    ],
)
async def test_scan_combines_persisted_results(
    persisted_store,
    agent_status,
    async_status,
    expected,
    error,
):
    db, store = persisted_store
    db.add_intent()
    db.add_execution(
        status=agent_status,
        async_status=async_status,
        error_message="agent failed" if agent_status != "success" else "",
    )

    updated = await store.reconcile_dispatched_executions(now_utc=NOW)

    row = db.intent()
    assert row["status"] == expected
    assert row["error_message"] == error
    assert updated == (expected != "dispatched")
    if expected == "pending":
        assert row["due_at"] == (NOW + timedelta(seconds=300)).isoformat(" ")
    if expected == "dispatched":
        assert row["locked_at"] == (NOW - timedelta(hours=3)).isoformat(" ")
        store._record_event_best_effort.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_stops_retrying_at_max_attempts(persisted_store):
    db, store = persisted_store
    db.add_intent(attempt_count=3)
    db.add_execution(dispatch_attempt=3, async_status="error")

    await store.reconcile_dispatched_executions(
        now_utc=NOW,
        retry_delay_seconds=45,
    )

    assert db.intent()["status"] == "failed"
    assert db.intent()["completed_at"] == NOW.isoformat(" ")


@pytest.mark.asyncio
async def test_auth_expired_fails_without_retry_or_worker_penalty(
    persisted_store,
):
    db, store = persisted_store
    db.add_intent()
    db.add_execution(
        status="error",
        error_message=(
            "cron auth user_info is expired; "
            "please refresh cron auth configuration"
        ),
    )

    assert await store.reconcile_dispatched_executions(now_utc=NOW) == 1

    row = db.intent()
    assert row["status"] == "failed"
    assert row["completed_at"] == NOW.isoformat(" ")
    assert row["due_at"] == NOW.isoformat(" ")
    assert row["error_message"].startswith("鉴权过期:")
    assert store._record_event_best_effort.await_args.kwargs["details"][
        "retry"
    ] is False

    feedback = await store.summarize_recent_completion_feedback(
        since=NOW - timedelta(minutes=5),
        now_utc=NOW,
        scope={
            "source_id": "source-a",
            "provider_id": "default",
            "model_id": "default",
        },
    )
    assert feedback["failure_count"] == 0

    db.add_intent(
        id=8,
        status="failed",
        completed_at=NOW,
        updated_at=NOW,
        error_message="agent failed",
    )
    feedback = await store.summarize_recent_completion_feedback(
        since=NOW - timedelta(minutes=5),
        now_utc=NOW,
        scope={
            "source_id": "source-a",
            "provider_id": "default",
            "model_id": "default",
        },
    )
    assert feedback["failure_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"dispatch_attempt": 2},
        {"dispatch_batch_id": "other"},
        {"dispatch_intent_id": 8},
        {"job_id": "other"},
        {"tenant_id": "other"},
    ],
)
async def test_scan_ignores_other_execution_identity(
    persisted_store,
    override,
):
    db, store = persisted_store
    db.add_intent()
    db.add_execution(async_status="success", **override)

    assert await store.reconcile_dispatched_executions(now_utc=NOW) == 0
    assert db.intent()["status"] == "dispatched"


@pytest.mark.asyncio
async def test_scan_respects_model_scope(persisted_store):
    db, store = persisted_store
    db.add_intent()
    db.add_execution(async_status="success")

    assert (
        await store.reconcile_dispatched_executions(
            now_utc=NOW,
            source_ids=["other"],
        )
        == 0
    )
    assert (
        await store.reconcile_dispatched_executions(
            now_utc=NOW,
            provider_id="other",
        )
        == 0
    )
    assert (
        await store.reconcile_dispatched_executions(
            now_utc=NOW,
            model_id="other",
        )
        == 0
    )
    assert db.intent()["status"] == "dispatched"


@pytest.mark.asyncio
async def test_duplicate_rows_and_scans_only_complete_once(persisted_store):
    db, store = persisted_store
    db.add_intent()
    db.add_execution(async_status="success")
    db.add_execution(id=43, async_status="success")

    assert await store.reconcile_dispatched_executions(now_utc=NOW) == 1
    assert await store.reconcile_dispatched_executions(now_utc=NOW) == 0
    store._record_event_best_effort.assert_awaited_once()


@pytest.mark.asyncio
async def test_attempt_change_during_scan_cannot_complete_retry(
    persisted_store,
):
    db, store = persisted_store
    db.add_intent()
    db.add_execution(async_status="success")
    db.before_update = lambda: db.connection.execute(
        "UPDATE swe_cron_dispatch_intents SET attempt_count=2 WHERE id=7",
    )

    assert await store.reconcile_dispatched_executions(now_utc=NOW) == 0
    assert db.intent()["status"] == "dispatched"
    assert db.intent()["attempt_count"] == 2
    store._record_event_best_effort.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_status,async_status",
    [("success", "success"), ("success", "error"), ("error", None)],
)
async def test_stale_recovery_preserves_current_terminal_result(
    persisted_store,
    agent_status,
    async_status,
):
    db, store = persisted_store
    db.add_intent()
    db.add_execution(status=agent_status, async_status=async_status)

    assert await store.recover_stale_dispatched_intents(now_utc=NOW) == 0
    assert db.intent()["status"] == "dispatched"


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt,expected", [(1, "pending"), (3, "failed")])
async def test_missing_subtask_uses_existing_deadline_and_clear_error(
    persisted_store,
    attempt,
    expected,
):
    db, store = persisted_store
    db.add_intent(attempt_count=attempt)
    db.add_execution(dispatch_attempt=attempt)

    assert await store.recover_stale_dispatched_intents(now_utc=NOW) == 1
    assert db.intent()["status"] == expected
    assert db.intent()["error_message"] == "获取子任务状态超时"
    assert (
        store._record_event_best_effort.await_args.kwargs["details"]["error"]
        == "获取子任务状态超时"
    )


@pytest.mark.asyncio
async def test_previous_attempt_success_does_not_prevent_stale_recovery(
    persisted_store,
):
    db, store = persisted_store
    db.add_intent(attempt_count=2)
    db.add_execution(async_status="success")

    assert await store.recover_stale_dispatched_intents(now_utc=NOW) == 1
    assert db.intent()["status"] == "pending"
    assert (
        db.intent()["error_message"]
        == "dispatch outcome unknown past stale timeout"
    )


@pytest.mark.asyncio
async def test_swe_receipt_is_accepted_without_finalizing_agent_success():
    store = MagicMock()
    store.accept_execution_feedback = AsyncMock(return_value=True)
    store.complete_from_execution = AsyncMock(return_value=True)
    store.update_batch_counts = AsyncMock()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=MagicMock(),
    )
    service.dispatch_ready_once = AsyncMock()

    accepted = await service.handle_execution_recorded(
        execution_id=42,
        status="success",
        job_id="job-1",
        tenant_id="tenant-1",
        source_id="source-a",
        meta={
            "cron_dispatch": {
                "intent_id": 7,
                "batch_id": "batch-1",
                "dispatch_attempt": 1,
            },
        },
        completed_at=NOW,
    )

    assert accepted is True
    store.complete_from_execution.assert_not_awaited()
    service.dispatch_ready_once.assert_not_awaited()
    assert (
        store.accept_execution_feedback.await_args.kwargs[
            "expected_attempt_count"
        ]
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"expected_attempt_count": 2},
        {"expected_batch_id": "other"},
        {"expected_job_id": "other"},
        {"expected_tenant_id": "other"},
        {"expected_source_id": "other"},
        {"intent_id": 8},
    ],
)
async def test_receipt_rejects_invalid_identity_without_mutating_intent(
    persisted_store,
    changes,
):
    db, store = persisted_store
    db.add_intent()
    before = db.intent()
    identity = {
        "intent_id": 7,
        "execution_id": 42,
        "expected_batch_id": "batch-1",
        "expected_job_id": "job-1",
        "expected_tenant_id": "tenant-1",
        "expected_source_id": "source-a",
        "expected_attempt_count": 1,
    }

    assert (
        await store.accept_execution_feedback(**(identity | changes)) is False
    )
    assert db.intent() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["dispatched", "completed", "pending", "failed"],
)
async def test_repeated_receipt_does_not_reset_current_attempt(
    persisted_store,
    status,
):
    db, store = persisted_store
    db.add_intent(status=status)
    before = db.intent()

    assert (
        await store.accept_execution_feedback(
            intent_id=7,
            execution_id=42,
            expected_batch_id="batch-1",
            expected_job_id="job-1",
            expected_tenant_id="tenant-1",
            expected_source_id="source-a",
            expected_attempt_count=1,
        )
        is True
    )
    assert db.intent() == before


@pytest.mark.asyncio
async def test_rejected_receipt_cannot_seed_execution_for_later_scan():
    sync = MagicMock()
    sync.find_execution_by_dispatch_identity = AsyncMock(return_value=None)
    sync.record_execution = AsyncMock(return_value=42)
    service = MagicMock()
    service.handle_execution_recorded = AsyncMock(return_value=False)
    request = ExecutionSyncRequest(
        job_id="job-1",
        tenant_id="tenant-1",
        source_id="wrong-source",
        actual_time=NOW,
        status="success",
        meta='{"cron_dispatch":{"intent_id":7,"batch_id":"batch-1","dispatch_attempt":1}}',
    )

    with pytest.raises(HTTPException):
        await cron_router.record_dispatch_execution(
            request,
            sync_service=sync,
            scheduling_service=service,
        )

    sync.record_execution.assert_not_awaited()


@pytest.mark.asyncio
async def test_later_monitor_result_completes_without_another_swe_receipt(
    persisted_store,
):
    db, store = persisted_store
    db.add_intent(locked_at=NOW)
    db.add_execution()
    assert await store.reconcile_dispatched_executions(now_utc=NOW) == 0
    assert db.intent()["locked_at"] == NOW.isoformat(" ")

    db.connection.execute(
        "UPDATE swe_cron_executions SET async_status='success' WHERE id=42",
    )

    assert (
        await store.reconcile_dispatched_executions(
            now_utc=NOW + timedelta(seconds=30),
        )
        == 1
    )
    assert db.intent()["status"] == "completed"


@pytest.mark.asyncio
async def test_failure_retry_delay_starts_when_scan_observes_result(
    persisted_store,
):
    db, store = persisted_store
    db.add_intent()
    db.add_execution(async_status="error")

    await store.reconcile_dispatched_executions(
        now_utc=NOW,
        retry_delay_seconds=45,
    )

    assert db.intent()["due_at"] == (NOW + timedelta(seconds=45)).isoformat(
        " ",
    )


@pytest.mark.asyncio
async def test_page_limit_does_not_expose_unprocessed_results_to_recovery(
    persisted_store,
    monkeypatch,
):
    db, store = persisted_store
    monkeypatch.setattr(intent_module, "EXECUTION_SCAN_LIMIT", 1)
    db.add_intent()
    db.add_execution(async_status="success")
    db.add_intent(id=8, job_id="job-2")
    db.add_execution(
        id=43,
        job_id="job-2",
        dispatch_intent_id=8,
        async_status="error",
    )

    assert await store.reconcile_dispatched_executions(now_utc=NOW) == 1
    assert await store.recover_stale_dispatched_intents(now_utc=NOW) == 0
    assert await store.reconcile_dispatched_executions(now_utc=NOW) == 1
    states = dict(
        db.connection.execute(
            "SELECT id,status FROM swe_cron_dispatch_intents",
        ),
    )
    assert states == {7: "completed", 8: "pending"}


@pytest.mark.asyncio
async def test_claim_fallback_does_not_reexecute_a_current_terminal_result(
    persisted_store,
):
    db, store = persisted_store
    db.add_intent()
    db.add_execution(async_status="success")
    cursor = db.cursor()
    scope = {"scope_filter_clause": "", "scope_filter_params": ()}
    times = {
        "stale_before": NOW - timedelta(seconds=600),
        "dispatched_stale_before": NOW - timedelta(seconds=7800),
        "normalized_now": NOW,
    }

    assert (
        await store._fetch_candidate_batch_ids(
            cursor,
            limit=10,
            **times,
            **scope,
        )
        == []
    )
    store._lock_candidate_batch = AsyncMock(return_value=True)
    assert (
        await store._claimable_intent_ids_for_batch(
            cursor,
            candidate_batch_id="batch-1",
            lock_owner="worker-1",
            limit=10,
            **times,
            **scope,
        )
        == []
    )


@pytest.mark.asyncio
async def test_exhausted_claim_fallback_waits_for_reconciliation(
    persisted_store,
):
    db, store = persisted_store
    db.add_intent(attempt_count=3)
    db.add_execution(dispatch_attempt=3, async_status="success")
    cursor = db.cursor()
    kwargs = {
        "dispatched_stale_before": NOW - timedelta(seconds=7800),
        "scope_filter_clause": "",
        "scope_filter_params": (),
    }
    assert await store._fetch_exhausted_dispatched_rows(cursor, **kwargs) == []

    db.connection.execute("UPDATE swe_cron_executions SET async_status=NULL")
    rows = await store._fetch_exhausted_dispatched_rows(cursor, **kwargs)
    assert len(rows) == 1
    await store._mark_exhausted_dispatched_rows_failed(
        cursor,
        exhausted_rows=rows,
        normalized_now=NOW,
    )
    assert db.intent()["status"] == "failed"
    assert db.intent()["error_message"] == "获取子任务状态超时"


@pytest.mark.asyncio
async def test_unexpired_missing_results_keep_the_original_dispatch_lock(
    persisted_store,
):
    db, store = persisted_store
    db.add_intent(locked_at=NOW)
    before = db.intent()

    assert await store.reconcile_dispatched_executions(now_utc=NOW) == 0
    assert await store.recover_stale_dispatched_intents(now_utc=NOW) == 0
    assert db.intent() == before


@pytest.mark.asyncio
async def test_scanner_does_not_reopen_a_cancelled_or_already_requeued_attempt(
    persisted_store,
):
    db, store = persisted_store
    db.add_intent()
    db.add_execution(async_status="success")
    db.before_update = lambda: db.connection.execute(
        "UPDATE swe_cron_dispatch_intents SET status='pending' WHERE id=7",
    )

    assert await store.reconcile_dispatched_executions(now_utc=NOW) == 0
    assert db.intent()["status"] == "pending"
    store._record_event_best_effort.assert_not_awaited()
