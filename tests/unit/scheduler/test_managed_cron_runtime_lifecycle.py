# -*- coding: utf-8 -*-
"""新版后台独立启动、先停止再关闭共库，缺少新结构不影响旧链路。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from tests.unit.scheduler.test_managed_cron_callbacks import CallbackDatabase
from tests.unit.scheduler.test_managed_cron_capacity import configure_pool


class Worker:
    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run_loop(self, *, stop_event):
        self.started.set()
        try:
            await stop_event.wait()
        finally:
            self.stopped.set()


@pytest.mark.asyncio
async def test_actual_app_lifespan_starts_and_stops_new_worker_inside_old_lifecycle(
    monkeypatch,
):
    from scheduler.app import _app
    from scheduler.app.services.cron.managed import runtime

    worker, order = Worker(), []

    @asynccontextmanager
    async def legacy(_app_instance):
        order.append("legacy_start")
        yield
        assert worker.stopped.is_set()
        order.append("legacy_stop")

    async def factory(_db):
        return worker

    monkeypatch.setattr(_app, "lifespan", legacy)
    monkeypatch.setattr(
        runtime,
        "get_db_connection",
        lambda: SimpleNamespace(is_connected=True),
    )
    monkeypatch.setattr(runtime, "create_managed_worker", factory)
    app = FastAPI()
    async with _app.app.router.lifespan_context(app):
        await asyncio.wait_for(worker.started.wait(), 2)
        assert app.state.managed_cron_runtime_ready
        assert not app.state.managed_cron_task.done()
    assert worker.stopped.is_set()
    assert app.state.managed_cron_task.done()
    assert not app.state.managed_cron_runtime_ready
    assert order == ["legacy_start", "legacy_stop"]


@pytest.mark.asyncio
async def test_new_runtime_configuration_failure_does_not_fail_old_startup_or_log_secrets(
    monkeypatch,
    caplog,
):
    from scheduler.app.services.cron.managed import runtime

    async def unavailable(_db):
        raise RuntimeError("private_password_in_raw_error")

    monkeypatch.setattr(
        runtime,
        "get_db_connection",
        lambda: SimpleNamespace(is_connected=True),
    )
    monkeypatch.setattr(runtime, "create_managed_worker", unavailable)
    app = FastAPI()
    async with runtime.managed_lifespan(app):
        assert not app.state.managed_cron_runtime_ready
        assert not hasattr(app.state, "managed_cron_task")
    assert "private_password_in_raw_error" not in caplog.text


def complete_test_schema():
    from scheduler.app.services.cron.managed.operations import utc_now

    db = CallbackDatabase()
    configure_pool(db, utc_now())
    db.conn.executescript("""
        ALTER TABLE swe_cron_dispatch_scope_leases ADD COLUMN locked_at TEXT;
        ALTER TABLE swe_cron_dispatch_scope_leases ADD COLUMN heartbeat_at TEXT;
        CREATE TABLE swe_cron_dispatch_model_worker_policy (
            source_id TEXT, provider_id TEXT, model_id TEXT, default_strategy_id TEXT,
            strategy_schedule TEXT, enabled INTEGER
        );
        CREATE TABLE swe_cron_dispatch_worker_strategy (
            strategy_id TEXT, min_workers INTEGER, baseline_workers INTEGER,
            max_workers INTEGER, adjust_interval_seconds INTEGER,
            feedback_window_seconds INTEGER, stale_execution_seconds INTEGER,
            error_rate_rules TEXT, enabled INTEGER
        );
    """)
    for field in (
        "worker_id TEXT",
        "previous_workers INTEGER",
        "baseline_workers INTEGER",
        "min_workers INTEGER",
        "max_workers INTEGER",
        "pending_count INTEGER",
        "claimed_count INTEGER",
        "running_count INTEGER",
        "success_count INTEGER",
        "failure_count INTEGER",
        "error_rate REAL",
        "matched_rule TEXT",
        "avg_latency_ms INTEGER",
        "decision_reason TEXT",
    ):
        db.conn.execute(
            f"ALTER TABLE swe_cron_dispatch_worker_capacity ADD COLUMN {field}",
        )
    return db


@pytest.mark.asyncio
async def test_schema_check_is_read_only_and_validates_columns_not_just_tables():
    from scheduler.app.services.cron.managed.schema import (
        ManagedSchemaUnavailable,
        assert_managed_schema_ready,
    )

    db = complete_test_schema()
    try:
        await assert_managed_schema_ready(db)
        assert db.writes == 0
        db.conn.execute(
            "ALTER TABLE swe_cron_execution_runs DROP COLUMN provider_id",
        )
        with pytest.raises(ManagedSchemaUnavailable) as error:
            await assert_managed_schema_ready(db)
        assert error.value.table == "swe_cron_execution_runs"
        assert db.writes == 0
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_cancelled_background_worker_does_not_interrupt_outer_shutdown(
    monkeypatch,
):
    from scheduler.app.services.cron.managed import runtime

    worker = Worker()

    async def factory(_db):
        return worker

    monkeypatch.setattr(
        runtime,
        "get_db_connection",
        lambda: SimpleNamespace(is_connected=True),
    )
    monkeypatch.setattr(runtime, "create_managed_worker", factory)
    app = FastAPI()
    async with runtime.managed_lifespan(app):
        await worker.started.wait()
        app.state.managed_cron_task.cancel()
        await asyncio.gather(
            app.state.managed_cron_task,
            return_exceptions=True,
        )
    assert worker.stopped.is_set()
    assert not app.state.managed_cron_runtime_ready


@pytest.mark.asyncio
async def test_existing_database_lifecycle_closes_even_when_app_body_raises(
    monkeypatch,
):
    from unittest.mock import AsyncMock
    from scheduler.app import _app

    close = AsyncMock()
    monkeypatch.setattr(_app, "DB_HOST", "test-db")
    monkeypatch.setattr(_app, "init_db_connection", AsyncMock())
    monkeypatch.setattr(_app, "close_db_connection", close)
    monkeypatch.setattr(_app, "cron_scheduling_runtime_enabled", lambda: False)
    monkeypatch.setattr(_app, "prepare_managed_runtime", AsyncMock())
    with pytest.raises(RuntimeError, match="request body failed"):
        async with _app.lifespan(FastAPI()):
            raise RuntimeError("request body failed")
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_accounting_is_ready_before_old_loop_can_claim(monkeypatch):
    from unittest.mock import AsyncMock
    from scheduler.app import _app
    from scheduler.app.services.cron import capacity_claim
    from scheduler.app.services.cron.managed import runtime

    db = complete_test_schema()
    old_started = asyncio.Event()

    class Legacy:
        def bind_managed_feedback(self, feedback):
            self.feedback = feedback

        async def run_loop(self, *, stop_event):
            assert capacity_claim.MANAGED_CAPACITY_ACCOUNTING
            assert self.feedback is not None
            old_started.set()
            await stop_event.wait()

    legacy = Legacy()
    monkeypatch.setattr(capacity_claim, "MANAGED_CAPACITY_ACCOUNTING", False)
    monkeypatch.setattr(_app, "DB_HOST", "test-db")
    monkeypatch.setattr(_app, "init_db_connection", AsyncMock(return_value=db))
    monkeypatch.setattr(_app, "close_db_connection", AsyncMock())
    monkeypatch.setattr(_app, "cron_scheduling_runtime_enabled", lambda: True)
    monkeypatch.setattr(_app, "assert_run_state_schema_ready", AsyncMock())
    monkeypatch.setattr(_app, "initialize_missing_controls", AsyncMock())
    monkeypatch.setattr(_app, "get_cron_scheduling_service", lambda: legacy)
    monkeypatch.setattr(runtime, "get_db_connection", lambda: db)
    monkeypatch.setattr(runtime, "get_cron_scheduling_service", lambda: legacy)
    monkeypatch.setenv("SCHEDULER_SWE_API_BASE_URL", "http://swe")
    monkeypatch.setenv("SWE_CRON_SCHEDULER_BASE_URL", "http://platform")
    monkeypatch.setenv("SWE_SERVER_DOMAIN", "http://gateway")
    app = FastAPI()
    try:
        async with _app.application_lifespan(app):
            await asyncio.wait_for(old_started.wait(), 2)
            assert app.state.managed_cron_runtime_ready
            assert (
                app.state.managed_cron_worker.dispatcher.capacity is not None
            )
        assert app.state.managed_cron_task.done()
        assert app.state.scheduler_task.done()
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_cancelling_shutdown_itself_still_reaps_child_and_propagates_cancellation():
    from scheduler.app.services.cron.managed import runtime

    stop = asyncio.Event()
    never = asyncio.Event()
    child = asyncio.create_task(never.wait())
    closing = asyncio.create_task(runtime._stop(child, stop))
    await stop.wait()
    closing.cancel()
    await asyncio.gather(closing, return_exceptions=True)
    assert closing.cancelled()
    assert child.done()


@pytest.mark.asyncio
async def test_schema_checks_fields_used_by_initial_capacity_snapshot_writer():
    from scheduler.app.services.cron.managed.schema import (
        ManagedSchemaUnavailable,
        assert_managed_schema_ready,
    )

    db = complete_test_schema()
    try:
        await assert_managed_schema_ready(db)
        db.conn.execute(
            "ALTER TABLE swe_cron_dispatch_worker_capacity DROP COLUMN worker_id",
        )
        with pytest.raises(ManagedSchemaUnavailable) as error:
            await assert_managed_schema_ready(db)
        assert error.value.table == "swe_cron_dispatch_worker_capacity"
    finally:
        db.conn.close()
