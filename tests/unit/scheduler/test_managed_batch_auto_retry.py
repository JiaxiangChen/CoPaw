# -*- coding: utf-8 -*-
"""新版批次按旧规则重试，原历史、批成员和执行身份保持可追溯。"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from tests.unit.scheduler.test_managed_batch_retry import (
    failed_batch,
    retry_body,
)
from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup


async def failed_setup(setup, detail="model service unavailable"):
    from scheduler.app.services.cron.managed.auto_retry import AutoBatchRetry

    caller, batch = await failed_batch(setup)
    db = setup[0]
    run = await db.fetch_one("SELECT * FROM swe_cron_execution_runs")
    await db.execute(
        "UPDATE swe_cron_execution_runs SET result_json=%s",
        (json.dumps({"error_detail": detail}),),
    )
    await db.execute(
        "UPDATE swe_cron_executions SET error_message=%s",
        (detail,),
    )
    now = datetime.fromisoformat(run["finished_at"])
    worker = AutoBatchRetry(db, clock=lambda: now)
    return db, caller, batch, run, now, worker


async def complete_run(db, run, status="error"):
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    writer = ExecutionWriter(db)
    key = tuple(
        run[name] for name in ("tenant_id", "source_id", "agent_id", "job_id")
    )
    due = datetime.fromisoformat(run["not_before_at"])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("swe.app.crons.managed_results.utc_now", lambda: due)
        await writer.accept(run["execution_id"], key)
        assert await writer.start(run["execution_id"], key, "retry-owner")
        await writer.complete(
            run["execution_id"],
            key,
            "retry-owner",
            ExecutionOutcome(status),
        )


@pytest.mark.asyncio
async def test_auto_retry_waits_five_minutes_and_replaces_history_on_completion(
    callback_setup,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from tests.unit.scheduler.test_managed_cron_dispatcher import SweClient

    db, _, _, original, now, worker = await failed_setup(callback_setup)
    assert await worker.tick() == 1
    assert await worker.tick() == 0
    retry = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id<>%s",
        (original["execution_id"],),
    )
    assert datetime.fromisoformat(retry["not_before_at"]) == now + timedelta(
        minutes=5,
    )
    assert retry["actor_id"] == original["actor_id"]
    meta = json.loads(retry["dispatch_meta_json"])
    assert meta["previous_execution_id"] == original["execution_id"]
    assert meta["business_attempt"] == 2
    dispatcher = ExecutionDispatcher(db, SweClient(db), clock=lambda: now)
    assert (await dispatcher.dispatch_registered(retry["execution_id"]))[
        "status"
    ] == "queued"
    assert (await db.fetch_one("SELECT is_final FROM swe_cron_executions"))[
        "is_final"
    ] == 1
    now += timedelta(minutes=5)
    assert (await dispatcher.dispatch_registered(retry["execution_id"]))[
        "status"
    ] == "accepted"
    await complete_run(db, retry, "success")
    rows = await db.fetch_all(
        "SELECT status,is_final FROM swe_cron_executions ORDER BY id",
    )
    assert [(row["status"], row["is_final"]) for row in rows] == [
        ("error", 0),
        ("success", 1),
    ]
    assert await worker.tick() == 0


@pytest.mark.asyncio
async def test_auto_retry_stops_after_three_business_attempts(callback_setup):
    db, _, _, _, _, worker = await failed_setup(callback_setup)
    for _ in range(2):
        assert await worker.tick() == 1
        run = await db.fetch_one(
            "SELECT * FROM swe_cron_execution_runs ORDER BY queue_order DESC LIMIT 1",
        )
        await complete_run(db, run)
    assert await worker.tick() == 0
    assert (
        await db.fetch_one("SELECT COUNT(*) AS n FROM swe_cron_execution_runs")
    )["n"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail",
    [
        "cron auth user_info is expired; please refresh cron auth configuration",
        "ValueError: workflow sapId does not match target user",
        "WorkflowCallError: 技能任务结果不可用",
    ],
)
async def test_terminal_errors_do_not_auto_retry(callback_setup, detail):
    db, _, _, _, _, worker = await failed_setup(callback_setup, detail)
    assert await worker.tick() == 0
    assert (
        await db.fetch_one("SELECT COUNT(*) AS n FROM swe_cron_execution_runs")
    )["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "UPDATE swe_cron_jobs_v2 SET enabled=0",
        "UPDATE swe_cron_jobs_v2 SET deleted_at='2026-10-22 00:00:00'",
        "UPDATE swe_cron_jobs_v2 SET definition_version=definition_version+1",
        "UPDATE swe_cron_job_states SET pause_reason='unread_limit'",
        "UPDATE swe_cron_job_states SET queue_invalidation_version=999",
    ],
)
async def test_auto_retry_does_not_revive_invalid_jobs(callback_setup, change):
    db, _, _, _, _, worker = await failed_setup(callback_setup)
    await db.execute(change)
    assert await worker.tick() == 0


@pytest.mark.asyncio
async def test_workers_and_manual_retry_cannot_add_duplicate_attempts(
    callback_setup,
):
    from scheduler.app.services.cron.managed.auto_retry import AutoBatchRetry
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db, caller, batch, _, _, worker = await failed_setup(callback_setup)
    manual = BatchRetryService(db)
    command = retry_body(await manual.preview(caller, batch))
    await asyncio.gather(
        worker.tick(),
        AutoBatchRetry(db).tick(),
        manual.retry(caller, batch, command),
    )
    assert (
        await db.fetch_one("SELECT COUNT(*) AS n FROM swe_cron_execution_runs")
    )["n"] == 2
    assert (
        await db.fetch_one(
            "SELECT business_attempt FROM swe_cron_managed_batch_members",
        )
    )["business_attempt"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "async_status,elapsed,expected",
    [
        ("error", 1, 1),
        ("pending", 7799, 0),
        ("pending", 7801, 1),
        ("success", 7801, 0),
    ],
)
async def test_async_failure_and_old_timeout_rule(
    callback_setup,
    async_status,
    elapsed,
    expected,
):
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    db, _, _, _, now, worker = await failed_setup(callback_setup)
    await db.execute(
        "UPDATE swe_cron_execution_runs SET status='success',error_code=NULL,result_json=NULL,started_at=%s",
        (now - timedelta(seconds=elapsed),),
    )
    await db.execute(
        "UPDATE swe_cron_executions SET status='success',async_status=%s",
        (async_status,),
    )
    await ExecutionSettlement(db, clock=lambda: now).tick()
    assert await worker.tick() == expected
    history = await db.fetch_one(
        "SELECT status,async_status FROM swe_cron_executions",
    )
    assert (history["status"], history["async_status"]) == (
        "success",
        async_status,
    )


@pytest.mark.asyncio
async def test_error_metadata_and_history_both_prevent_terminal_retry(
    callback_setup,
):
    db, _, _, _, _, worker = await failed_setup(
        callback_setup,
        "技能任务结果不可用",
    )
    await db.execute("UPDATE swe_cron_execution_runs SET result_json='{}'")
    assert await worker.tick() == 0
    await db.execute("UPDATE swe_cron_executions SET error_message='' ")
    await db.execute(
        "UPDATE swe_cron_execution_runs SET result_json=%s",
        (json.dumps({"failure_type": "auth_expired"}),),
    )
    assert await worker.tick() == 0


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_member_pointer_and_new_execution(
    callback_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed import auto_retry

    db, _, _, _, _, worker = await failed_setup(callback_setup)

    async def fail_event(*args, **kwargs):
        raise RuntimeError("event storage unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(auto_retry, "append_run_event", fail_event)
        assert await worker.tick() == 0
    assert (
        await db.fetch_one("SELECT COUNT(*) AS n FROM swe_cron_execution_runs")
    )["n"] == 1
    assert (
        await db.fetch_one(
            "SELECT business_attempt FROM swe_cron_managed_batch_members",
        )
    )["business_attempt"] == 1
    assert await worker.tick() == 1


@pytest.mark.asyncio
async def test_background_runtime_enqueues_automatic_retry(callback_setup):
    from tests.unit.scheduler.test_managed_cron_recovery import (
        RecoveryClient,
        worker as runtime_worker,
    )

    db, _, _, _, _, _ = await failed_setup(callback_setup)
    runtime = runtime_worker(db, callback_setup[2], RecoveryClient(db))
    assert (await runtime.tick())["retried"] == 1
    assert (
        await db.fetch_one(
            "SELECT business_attempt FROM swe_cron_managed_batch_members",
        )
    )["business_attempt"] == 2


@pytest.mark.asyncio
async def test_last_attempt_subtask_timeout_settles_without_fourth_attempt(
    callback_setup,
):
    db, _, _, original, now, worker = await failed_setup(callback_setup)
    await db.execute(
        "UPDATE swe_cron_managed_batch_members SET business_attempt=3",
    )
    await db.execute(
        "UPDATE swe_cron_execution_runs SET status='success',error_code=NULL,result_json=NULL,started_at=%s",
        (now - timedelta(seconds=7801),),
    )
    await db.execute(
        "UPDATE swe_cron_executions SET status='success',async_status='pending'",
    )
    assert await worker.tick() == 0
    run = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (original["execution_id"],),
    )
    assert run["settled_status"] == "error"
    assert run["settled_error_code"] == "cron_async_subtasks_timeout"
    assert (
        await db.fetch_one("SELECT COUNT(*) AS n FROM swe_cron_execution_runs")
    )["n"] == 1
