# -*- coding: utf-8 -*-
"""验证后台只恢复已有登记，沿用真实身份且不重复执行业务。"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.scheduler.test_managed_cron_dispatcher import SweClient
from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)
from tests.unit.scheduler.test_managed_cron_dispatch_state import mode_setup


class RecoveryClient(SweClient):
    def __init__(self, db, **kwargs):
        super().__init__(db, **kwargs)
        self.actors = []
        self.recoveries = []

    async def execute(self, row, caller):
        self.actors.append(caller)
        return await super().execute(row, caller)

    async def recover(self, row, caller):
        self.recoveries.append((row["execution_id"], caller))
        return {"execution_id": row["execution_id"], "status": "running"}


@pytest.mark.asyncio
async def test_registered_dispatch_uses_saved_actor_without_elevating_role(
    execution_setup,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )

    db, _, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET actor_id='platform-actor'",
    )
    client = RecoveryClient(db)
    result = await ExecutionDispatcher(db, client).dispatch_registered(
        row["execution_id"],
    )
    assert result["status"] == "accepted"
    assert client.actors[0].actor_id == "platform-actor"
    assert client.actors[0].tenant_id == "alice"
    assert not client.actors[0].is_manager
    assert (
        json.loads(row["dispatch_meta_json"])["protocol"]
        == "scheduler_managed_v1"
    )


@pytest.mark.asyncio
async def test_registered_worker_excludes_legacy_protocol_and_future_runs(
    execution_setup,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )

    db, _, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    now = datetime(2026, 10, 16, 2)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET not_before_at=?",
        ((now + timedelta(hours=1)).isoformat(),),
    )
    client = RecoveryClient(db)
    dispatcher = ExecutionDispatcher(db, client, clock=lambda: now)
    result = await dispatcher.dispatch_registered(row["execution_id"])
    assert result["status"] == "queued"
    assert not client.calls
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET dispatch_meta_json='{}'",
    )
    assert await dispatcher.dispatch_registered(row["execution_id"]) is None
    assert not client.calls


@pytest.mark.asyncio
async def test_transport_retry_waits_then_reuses_original_execution(
    execution_setup,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )

    db, _, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    client = RecoveryClient(db, accept=False)
    dispatcher = ExecutionDispatcher(db, client, clock=lambda: now)
    for _ in range(4):
        result = await dispatcher.dispatch_registered(row["execution_id"])
        await dispatcher.dispatch_registered(row["execution_id"])
        now += timedelta(minutes=2)
    assert result["status"] == "dispatch_failed"
    assert client.calls == [row["execution_id"]] * 4
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_running_recovery_calls_result_recovery_not_execution(
    execution_setup,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from swe.app.crons.managed_results import ExecutionWriter

    db, caller, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    writer = ExecutionWriter(db)
    await writer.accept(row["execution_id"], caller.job_key(row["job_id"]))
    await writer.start(
        row["execution_id"],
        caller.job_key(row["job_id"]),
        "runner",
    )
    client = RecoveryClient(db)
    dispatcher = ExecutionDispatcher(db, client)
    result = await dispatcher.recover_registered(row["execution_id"])
    await dispatcher.recover_registered(row["execution_id"])
    assert result["status"] == "running"
    assert not client.calls
    assert len(client.recoveries) == 1


def worker(db, management, client):
    from scheduler.app.services.cron.managed.recovery import (
        ManagedRecoveryWorker,
    )

    db.conn.create_function("JSON_UNQUOTE", 1, lambda value: value)
    return ManagedRecoveryWorker(
        db,
        management.resources,
        management.platform,
        client,
    )


@pytest.mark.asyncio
async def test_worker_dispatches_only_registered_runs_and_never_scans_definitions(
    mode_setup,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from tests.unit.scheduler.test_managed_cron_management import (
        create_payload,
    )

    db, caller, management = mode_setup
    created = await management.create(caller, create_payload())
    client = RecoveryClient(db)
    runtime = worker(db, management, client)
    await runtime.tick()
    assert not client.calls
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 0
    )
    run, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest("manual", "click-1"),
    )
    result = await runtime.tick()
    assert result["dispatched"] == 1
    assert client.calls == [run["execution_id"]]
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='error', dispatch_lease_until=NULL",
    )
    await runtime.tick()
    assert client.calls == [run["execution_id"]]


@pytest.mark.asyncio
async def test_worker_resumes_async_broadcast_and_mode_but_does_not_retry_failed_management(
    mode_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )
    from tests.unit.scheduler.test_managed_cron_distribution import payload
    from tests.unit.scheduler.test_managed_cron_management import (
        create_payload,
    )

    db, caller, management = mode_setup
    created = await management.create(caller, create_payload())
    task = await BroadcastStore(db).submit(
        caller,
        created["job_id"],
        payload(),
    )
    runtime = worker(db, management, RecoveryClient(db))
    await runtime.tick()
    view = await BroadcastStore(db).get(
        caller,
        created["job_id"],
        task["task_id"],
    )
    assert view["status"] == "succeeded"
    mode = DispatchStateService(db, management.platform)
    current = await mode.get(caller, created["job_id"])
    pending = await mode.set(
        caller,
        created["job_id"],
        {
            "request_id": "new-mode",
            "state": "batch_enabled",
            "expected_version": current["version"],
        },
    )
    await runtime.tick()
    assert (await mode.get(caller, created["job_id"]))[
        "sync_status"
    ] == "completed"
    calls = len(management.platform.trace)
    db.conn.execute(
        "UPDATE swe_cron_registration_journal SET status='failed' WHERE operation_id=?",
        (pending["operation_id"],),
    )
    await runtime.tick()
    assert len(management.platform.trace) == calls


@pytest.mark.asyncio
async def test_cleanup_waiting_resumes_after_inflight_finishes_and_preserves_admin_actor(
    mode_setup,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter
    from tests.unit.scheduler.test_managed_cron_management import (
        create_payload,
    )

    db, owner, management = mode_setup
    created = await management.create(owner, create_payload())
    run, _ = await ExecutionStore(db).enqueue(
        owner,
        created["job_id"],
        ExecutionRequest("manual", "click"),
    )
    key = owner.job_key(created["job_id"])
    writer = ExecutionWriter(db)
    await writer.accept(run["execution_id"], key)
    await writer.start(run["execution_id"], key, "runner")
    admin = CallerIdentity("alice", "bank", "default", "real-admin", True)
    pending = await management.delete(
        admin,
        created["job_id"],
        {"request_id": "delete-running", "expected_definition_version": 1},
    )
    assert pending["stage"] == "cleanup_waiting"
    runtime = worker(db, management, RecoveryClient(db))
    await runtime.tick()
    assert not management.resources.cleaned
    await writer.complete(
        run["execution_id"],
        key,
        "runner",
        ExecutionOutcome("success"),
    )
    await runtime.tick()
    assert not management.resources.cleaned
    # 模拟既有 Monitor 按真实子任务状态完成历史异步字段，新链路只观察共库。
    db.conn.execute(
        "UPDATE swe_cron_executions SET async_status='success' WHERE status='success'",
    )
    await runtime.tick()
    await runtime.tick()
    operation = await management.store.get(
        admin,
        created["job_id"],
        pending["operation_id"],
    )
    assert operation["status"] == "completed"
    assert operation["actor_id"] == "real-admin"
    assert json.loads(operation["payload_json"])["actor_is_manager"] is True


@pytest.mark.asyncio
async def test_long_broadcast_does_not_block_execution_lane(mode_setup):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from tests.unit.scheduler.test_managed_cron_management import (
        create_payload,
    )
    from tests.unit.scheduler.test_managed_cron_distribution import payload

    db, caller, management = mode_setup
    created = await management.create(caller, create_payload())
    await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest("manual", "click"),
    )
    await BroadcastStore(db).submit(caller, created["job_id"], payload())
    client = RecoveryClient(db)
    runtime = worker(db, management, client)
    entered, release, dispatched, stop = (asyncio.Event() for _ in range(4))
    original = client.execute

    async def slow_broadcast(_task_id):
        entered.set()
        await release.wait()

    async def execute(*args):
        result = await original(*args)
        dispatched.set()
        return result

    runtime.broadcast.process = slow_broadcast
    client.execute = execute
    task = asyncio.create_task(
        runtime.run_loop(stop_event=stop, interval_seconds=0.01),
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(dispatched.wait(), 2)
        assert not release.is_set()
        assert len(client.calls) == 1
    finally:
        stop.set()
        release.set()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_http_recovery_commits_saved_result_without_running_engine_twice(
    execution_setup,
    monkeypatch,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
        ManagedSweExecutionClient,
    )
    from swe.app.crons import managed_execution_api
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_receipts import SessionResultReceipts
    from swe.app.crons.managed_results import ExecutionWriter
    from tests.unit.app.test_managed_cron_execution import Engine
    from tests.unit.app.test_managed_cron_result_recovery import Session

    db, _, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    started, release = asyncio.Event(), asyncio.Event()
    engine = Engine(started=started, release=release)
    receipts = SessionResultReceipts(Session())
    failed_writer = ExecutionWriter(db)

    async def unavailable(*_args):
        raise ConnectionError("private database credentials")

    monkeypatch.setattr(failed_writer, "complete", unavailable)
    services = [
        ManagedExecutionService(failed_writer, engine, receipts=receipts),
    ]

    async def service(_request):
        return services[0]

    monkeypatch.setattr(
        managed_execution_api,
        "get_execution_service",
        service,
    )
    app = FastAPI()
    app.state.db_connection = db
    app.include_router(managed_execution_api.router, prefix="/api")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        dispatcher = ExecutionDispatcher(
            db,
            ManagedSweExecutionClient("http://test", http=http),
        )
        await dispatcher.dispatch_registered(row["execution_id"])
        await asyncio.wait_for(started.wait(), 2)
        tasks = set(app.state.managed_cron_tasks)
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert (
            db.conn.execute(
                "SELECT status FROM swe_cron_execution_runs",
            ).fetchone()[0]
            == "running"
        )
        services[0] = ManagedExecutionService(
            ExecutionWriter(db),
            engine,
            receipts=receipts,
        )
        db.conn.execute(
            "UPDATE swe_cron_execution_runs SET dispatch_lease_until=NULL",
        )
        result = await dispatcher.recover_registered(row["execution_id"])
        assert result["status"] == "success"
    assert len(engine.calls) == 1
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_executions").fetchone()[
            0
        ]
        == 1
    )
