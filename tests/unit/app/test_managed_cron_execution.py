# -*- coding: utf-8 -*-
"""验证 SWE 执行写入，不依赖 CronManager 任务文件或旧状态缓存。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)


def key(row):
    return tuple(
        row[name]
        for name in (
            "tenant_id",
            "source_id",
            "agent_id",
            "job_id",
        )
    )


class Engine:
    def __init__(self, *, failure=False, started=None, release=None):
        self.calls = []
        self.failure = failure
        self.started = started
        self.release = release

    async def execute(self, job, dispatch_meta=None):
        from swe.app.crons.execution_result import ExecutionResult

        self.calls.append((job, dispatch_meta))
        if self.started is not None:
            self.started.set()
            await self.release.wait()
        if self.failure:
            raise RuntimeError("private token=do-not-expose")
        return ExecutionResult(
            trace_id="trace-one",
            output_preview="report result",
        )


@pytest.mark.asyncio
async def test_repeated_acceptance_and_result_write_are_idempotent(
    execution_setup,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    engine = Engine()
    service = ManagedExecutionService(ExecutionWriter(db), engine)
    await service.execute(row["execution_id"], key(row))
    await service.execute(row["execution_id"], key(row))
    assert len(engine.calls) == 1
    run = db.conn.execute("SELECT * FROM swe_cron_execution_runs").fetchone()
    assert run["status"] == "success"
    assert run["history_execution_id"] is not None
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_executions").fetchone()[
            0
        ]
        == 1
    )
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    assert state["unread_execution_count"] == 1
    assert state["has_scheduled_result"] == 1
    assert state["latest_scheduled_preview"] == "report res"


@pytest.mark.asyncio
async def test_concurrent_swe_instances_do_not_start_one_execution_twice(
    execution_setup,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    started, release = asyncio.Event(), asyncio.Event()
    engine = Engine(started=started, release=release)
    first = asyncio.create_task(
        ManagedExecutionService(
            ExecutionWriter(db),
            engine,
        ).execute(row["execution_id"], key(row)),
    )
    await started.wait()
    await ManagedExecutionService(ExecutionWriter(db), engine).execute(
        row["execution_id"],
        key(row),
    )
    release.set()
    await first
    assert len(engine.calls) == 1


@pytest.mark.asyncio
async def test_actual_failure_is_terminal_not_automatically_replayed(
    execution_setup,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    engine = Engine(failure=True)
    service = ManagedExecutionService(ExecutionWriter(db), engine)
    await service.execute(row["execution_id"], key(row))
    await service.execute(row["execution_id"], key(row))
    run = dict(
        db.conn.execute("SELECT * FROM swe_cron_execution_runs").fetchone(),
    )
    assert run["status"] == "error"
    assert "do-not-expose" not in json.dumps(run)
    assert len(engine.calls) == 1


@pytest.mark.asyncio
async def test_queued_execution_is_skipped_after_disable(execution_setup):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    db.conn.execute("UPDATE swe_cron_jobs_v2 SET enabled=0")
    engine = Engine()
    await ManagedExecutionService(ExecutionWriter(db), engine).execute(
        row["execution_id"],
        key(row),
    )
    assert not engine.calls
    assert (
        db.conn.execute(
            "SELECT status FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == "skipped"
    )


@pytest.mark.asyncio
async def test_manual_success_updates_sidebar_and_starts_unread_once(
    execution_setup,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup, kind="manual")
    service = ManagedExecutionService(ExecutionWriter(db), Engine())
    await service.execute(
        row["execution_id"],
        key(row),
    )
    await service.execute(row["execution_id"], key(row))
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    assert state["unread_execution_count"] == 1
    assert state["has_scheduled_result"] == 1
    assert state["last_scheduled_run_at"]
    assert state["last_run_at"]
    assert state["latest_scheduled_preview"] == "report res"
    history = db.conn.execute("SELECT * FROM swe_cron_executions").fetchone()
    assert history["is_manual"] == 1
    assert history["is_read"] == 0
    assert history["read_at"] is None
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_executions").fetchone()[
            0
        ]
        == 1
    )


@pytest.mark.asyncio
async def test_manual_unread_accumulates_and_existing_mark_read_clears_it(
    execution_setup,
):
    from scheduler.app.services.cron.managed.controls import ControlService
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db, caller, management = execution_setup
    service = ManagedExecutionService(ExecutionWriter(db), Engine())
    first, _ = await enqueue(
        execution_setup,
        kind="manual",
        trigger="manual-1",
    )
    await service.execute(first["execution_id"], key(first))
    second, _ = await enqueue(
        execution_setup,
        kind="manual",
        trigger="manual-2",
    )
    await service.execute(second["execution_id"], key(second))
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    assert state["unread_execution_count"] == 2
    assert state["last_scheduled_run_at"]
    result = await ControlService(db, management.platform).mark_read(
        caller,
        first["job_id"],
        {"request_id": "read-manual-results"},
    )
    assert result["status"] == "completed"
    assert (
        db.conn.execute(
            "SELECT unread_execution_count FROM swe_cron_job_states",
        ).fetchone()[0]
        == 0
    )
    histories = db.conn.execute(
        "SELECT is_read,read_at FROM swe_cron_executions",
    ).fetchall()
    assert len(histories) == 2
    assert all(
        history["is_read"] == 1 and history["read_at"] for history in histories
    )


@pytest.mark.asyncio
async def test_manual_unread_does_not_directly_trigger_automatic_pause(
    execution_setup,
    monkeypatch,
):
    from swe.app.crons import managed_results
    from swe.app.crons.managed_execution import ManagedExecutionService

    monkeypatch.setattr(
        managed_results,
        "resolve_cron_unread_auto_pause_config",
        lambda _: SimpleNamespace(enabled=True, threshold=1),
    )
    db = execution_setup[0]
    run, _ = await enqueue(execution_setup, kind="manual")
    await ManagedExecutionService(
        managed_results.ExecutionWriter(db),
        Engine(),
    ).execute(run["execution_id"], key(run))
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    assert state["unread_execution_count"] == 1
    assert not state["pause_reason"]


@pytest.mark.asyncio
async def test_manual_success_replaces_older_automatic_result_preview(
    execution_setup,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db = execution_setup[0]
    writer = ExecutionWriter(db)
    automatic, _ = await enqueue(
        execution_setup,
        kind="ordinary",
        trigger="auto-before-manual",
    )
    await ManagedExecutionService(writer, Engine()).execute(
        automatic["execution_id"],
        key(automatic),
    )
    manual, _ = await enqueue(
        execution_setup,
        kind="manual",
        trigger="manual-after-auto",
    )
    await writer.accept(manual["execution_id"], key(manual))
    await writer.start(manual["execution_id"], key(manual), "manual-owner")
    await writer.complete(
        manual["execution_id"],
        key(manual),
        "manual-owner",
        ExecutionOutcome("success", preview="manual now"),
    )
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    assert state["latest_scheduled_preview"] == "manual now"
    assert state["unread_execution_count"] == 2


@pytest.mark.asyncio
async def test_stale_definition_completion_does_not_overwrite_current_state(
    execution_setup,
):
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    writer = ExecutionWriter(db)
    await writer.accept(row["execution_id"], key(row))
    await writer.start(row["execution_id"], key(row), "worker")
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET definition_version=2, name='new-definition', enabled=0",
    )
    db.conn.execute(
        "UPDATE swe_cron_job_states SET management_status='update_failed', management_version=99, pause_reason='manual'",
    )
    await writer.complete(
        row["execution_id"],
        key(row),
        "worker",
        ExecutionOutcome("success", preview="old result"),
    )
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    assert state["management_status"] == "update_failed"
    assert state["management_version"] == 99
    assert state["pause_reason"] == "manual"
    assert state["unread_execution_count"] == 1
    definition = db.conn.execute("SELECT * FROM swe_cron_jobs_v2").fetchone()
    assert definition["name"] == "new-definition"
    assert definition["definition_version"] == 2
    assert not definition["enabled"]
    assert (
        db.conn.execute(
            "SELECT status FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == "success"
    )


@pytest.mark.asyncio
async def test_old_run_cannot_publish_after_its_owner_is_fenced(
    execution_setup,
):
    from swe.app.crons.managed_results import (
        ExecutionLeaseLost,
        ExecutionOutcome,
        ExecutionWriter,
    )

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    writer = ExecutionWriter(db)
    await writer.accept(row["execution_id"], key(row))
    await writer.start(row["execution_id"], key(row), "old-owner")
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET run_owner='fenced-owner'",
    )
    with pytest.raises(ExecutionLeaseLost):
        await writer.complete(
            row["execution_id"],
            key(row),
            "old-owner",
            ExecutionOutcome("success"),
        )
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_executions").fetchone()[
            0
        ]
        == 0
    )


@pytest.mark.asyncio
async def test_history_failure_rolls_back_result_and_does_not_claim_success(
    execution_setup,
    monkeypatch,
):
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    writer = ExecutionWriter(db)
    await writer.accept(row["execution_id"], key(row))
    await writer.start(row["execution_id"], key(row), "owner")
    original = db.execute

    async def failed(query, params=None):
        if query.startswith("INSERT INTO swe_cron_executions"):
            raise RuntimeError("history storage unavailable")
        return await original(query, params)

    monkeypatch.setattr(db, "execute", failed)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await writer.complete(
            row["execution_id"],
            key(row),
            "owner",
            ExecutionOutcome("success"),
        )
    assert (
        db.conn.execute(
            "SELECT status FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == "running"
    )
    assert not db.conn.execute(
        "SELECT unread_execution_count FROM swe_cron_job_states",
    ).fetchone()[0]


@pytest.mark.asyncio
async def test_auto_pause_writes_only_state_and_never_definition(
    execution_setup,
    monkeypatch,
):
    from swe.app.crons import managed_results
    from swe.app.crons.managed_execution import ManagedExecutionService

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    monkeypatch.setattr(
        managed_results,
        "resolve_cron_unread_auto_pause_config",
        lambda _: SimpleNamespace(enabled=True, threshold=1),
    )
    await ManagedExecutionService(
        managed_results.ExecutionWriter(db),
        Engine(),
    ).execute(row["execution_id"], key(row))
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    assert state["pause_reason"] == "auto_unread_threshold"
    assert (
        db.conn.execute("SELECT enabled FROM swe_cron_jobs_v2").fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_finishing_after_delete_does_not_resurrect_and_clears_running(
    execution_setup,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db, caller, management = execution_setup
    row, _ = await enqueue(execution_setup)
    started, release = asyncio.Event(), asyncio.Event()
    service = ManagedExecutionService(
        ExecutionWriter(db),
        Engine(started=started, release=release),
    )
    task = asyncio.create_task(service.execute(row["execution_id"], key(row)))
    await started.wait()
    deleted = await management.delete(
        caller,
        row["job_id"],
        {
            "request_id": "delete-inflight",
            "expected_definition_version": 1,
        },
    )
    assert deleted["stage"] == "cleanup_waiting"
    release.set()
    await task
    definition = db.conn.execute("SELECT * FROM swe_cron_jobs_v2").fetchone()
    assert definition["deleted_at"]
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    assert state["last_status"] != "running"
    assert not state["unread_execution_count"]
    history = db.conn.execute("SELECT * FROM swe_cron_executions").fetchone()
    assert history["notification_status"] == "not_required"


@pytest.mark.asyncio
async def test_queued_newer_run_does_not_leave_completed_run_showing_running(
    execution_setup,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    started, release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        ManagedExecutionService(
            ExecutionWriter(db),
            Engine(started=started, release=release),
        ).execute(row["execution_id"], key(row)),
    )
    await started.wait()
    await enqueue(execution_setup, trigger="period-2")
    release.set()
    await task
    assert (
        db.conn.execute(
            "SELECT last_status FROM swe_cron_job_states",
        ).fetchone()[0]
        == "success"
    )


@pytest.mark.asyncio
async def test_cleanup_checks_every_active_run_not_only_aggregate_state(
    execution_setup,
):
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db, caller, management = execution_setup
    row, _ = await enqueue(execution_setup)
    writer = ExecutionWriter(db)
    await writer.accept(row["execution_id"], key(row))
    await writer.start(row["execution_id"], key(row), "owner")
    db.conn.execute("UPDATE swe_cron_job_states SET last_status='success'")
    deleted = await management.delete(
        caller,
        row["job_id"],
        {
            "request_id": "delete-aggregate-mismatch",
            "expected_definition_version": 1,
        },
    )
    assert deleted["stage"] == "cleanup_waiting"
    await writer.complete(
        row["execution_id"],
        key(row),
        "owner",
        ExecutionOutcome("success"),
    )
    # 主执行成功后仍等共库异步终态，不能仅凭聚合状态提前清理资源。
    db.conn.execute(
        "UPDATE swe_cron_executions SET async_status='success' WHERE status='success'",
    )
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    await ExecutionSettlement(db).tick()
    done = await management.retry(
        caller,
        row["job_id"],
        deleted["operation_id"],
    )
    assert done["status"] == "completed"


@pytest.mark.asyncio
async def test_swe_can_accept_late_request_after_dispatch_timeout(
    execution_setup,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='dispatch_failed', error_code='cron_dispatch_unconfirmed'",
    )
    engine = Engine()
    await ManagedExecutionService(ExecutionWriter(db), engine).execute(
        row["execution_id"],
        key(row),
    )
    assert len(engine.calls) == 1
    current = db.conn.execute(
        "SELECT * FROM swe_cron_execution_runs",
    ).fetchone()
    assert current["status"] == "success"
    assert current["error_code"] is None


@pytest.mark.asyncio
async def test_runtime_metadata_redacts_cookie_and_credential_fragments(
    execution_setup,
):
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    writer = ExecutionWriter(db)
    await writer.accept(row["execution_id"], key(row))
    await writer.start(row["execution_id"], key(row), "owner")
    await writer.complete(
        row["execution_id"],
        key(row),
        "owner",
        ExecutionOutcome(
            "error",
            execution_meta={
                "passthrough_headers": {
                    "X-Header-Cookie": "private-cookie-value",
                },
                "detail": "api_key=private-api-key-value",
            },
        ),
    )
    current = db.conn.execute(
        "SELECT * FROM swe_cron_execution_runs",
    ).fetchone()
    history = db.conn.execute("SELECT * FROM swe_cron_executions").fetchone()
    assert (
        "private-cookie-value" not in current["result_json"] + history["meta"]
    )
    assert (
        "private-api-key-value" not in current["result_json"] + history["meta"]
    )


@pytest.mark.asyncio
async def test_large_history_metadata_keeps_identity_fields(execution_setup):
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    writer = ExecutionWriter(db)
    await writer.accept(row["execution_id"], key(row))
    await writer.start(row["execution_id"], key(row), "owner")
    await writer.complete(
        row["execution_id"],
        key(row),
        "owner",
        ExecutionOutcome("success", execution_meta={"detail": "x" * 8000}),
    )
    history = db.conn.execute("SELECT * FROM swe_cron_executions").fetchone()
    meta = json.loads(history["meta"])
    assert meta["managed_execution_id"] == row["execution_id"]
    assert meta["source_id"] == row["source_id"]
    assert len(history["meta"]) <= 2048


@pytest.mark.asyncio
async def test_swe_acceptance_does_not_write_scheduler_dispatch_lease(
    execution_setup,
):
    from swe.app.crons.managed_results import ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='dispatching', dispatch_owner='scheduler-worker', dispatch_lease_until='2099-01-01 00:00:00'",
    )
    accepted = await ExecutionWriter(db).accept(row["execution_id"], key(row))
    assert accepted["status"] == "accepted"
    assert accepted["dispatch_owner"] == "scheduler-worker"


@pytest.mark.asyncio
async def test_expired_running_execution_is_fenced_without_reexecution(
    execution_setup,
):
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    writer = ExecutionWriter(db)
    await writer.accept(row["execution_id"], key(row))
    await writer.start(row["execution_id"], key(row), "old-worker")
    assert not await writer.expire(row["execution_id"], key(row))
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET run_lease_until='2000-01-01 00:00:00'",
    )
    assert await writer.expire(row["execution_id"], key(row))
    assert not await writer.complete(
        row["execution_id"],
        key(row),
        "old-worker",
        ExecutionOutcome("success", preview="late"),
    )
    current = db.conn.execute(
        "SELECT * FROM swe_cron_execution_runs",
    ).fetchone()
    assert current["status"] == "error"
    assert current["error_code"] == "cron_execution_lease_expired"
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_executions").fetchone()[
            0
        ]
        == 1
    )
