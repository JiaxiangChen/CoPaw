# -*- coding: utf-8 -*-
"""共库观察异步终态，Scheduler 只写自己的收敛字段。"""

import json
from datetime import datetime, timezone

import pytest

from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)


async def completed(execution_setup, async_status=None, *, status="success"):
    db, _, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    metadata = {
        "management_owner": "scheduler",
        "managed_execution_id": row["execution_id"],
        "source_id": row["source_id"],
        "agent_id": row["agent_id"],
    }
    await db.execute(
        "INSERT INTO swe_cron_executions (job_id,tenant_id,status,async_status,meta) VALUES (%s,%s,%s,%s,%s)",
        (
            row["job_id"],
            row["tenant_id"],
            status,
            async_status,
            json.dumps(metadata),
        ),
    )
    history = await db.fetch_one("SELECT LAST_INSERT_ID() AS id")
    await db.execute(
        "UPDATE swe_cron_execution_runs SET status=%s,history_execution_id=%s,started_at='2026-10-16 01:00:00',finished_at='2026-10-16 01:01:00' WHERE execution_id=%s",
        (status, history["id"], row["execution_id"]),
    )
    return row, history["id"]


@pytest.mark.asyncio
async def test_primary_success_waits_for_shared_history_async_result(
    execution_setup,
):
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    db, _, _ = execution_setup
    row, history = await completed(execution_setup)
    service = ExecutionSettlement(db)
    assert await service.tick() == 0
    current = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (row["execution_id"],),
    )
    assert current["status"] == "success"
    assert current["settled_status"] is None
    await db.execute(
        "UPDATE swe_cron_executions SET async_status='success' WHERE id=%s",
        (history,),
    )
    assert await service.tick() == 1
    assert await service.tick() == 0
    settled = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (row["execution_id"],),
    )
    assert settled["status"] == "success"
    assert settled["settled_status"] == "success"
    assert settled["settled_at"]
    assert settled["finished_at"] == "2026-10-16 01:01:00"
    from scheduler.app.services.cron.managed.executions import (
        execution_summary,
    )

    assert execution_summary(settled)["settled_status"] == "success"


@pytest.mark.asyncio
async def test_async_failure_does_not_rewrite_swe_primary_outcome(
    execution_setup,
):
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    db, _, _ = execution_setup
    row, _ = await completed(execution_setup, "error")
    assert await ExecutionSettlement(db).tick() == 1
    current = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (row["execution_id"],),
    )
    assert current["status"] == "success"
    assert current["settled_status"] == "error"
    assert current["settled_error_code"] == "cron_async_subtasks_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "foreign_field",
    [
        "tenant_id",
        "job_id",
        "management_owner",
        "source_id",
        "agent_id",
        "managed_execution_id",
    ],
)
async def test_foreign_history_is_not_a_terminal_fact(
    execution_setup,
    foreign_field,
):
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    db, _, _ = execution_setup
    _, history = await completed(execution_setup, "success")
    if foreign_field in {"tenant_id", "job_id"}:
        await db.execute(
            f"UPDATE swe_cron_executions SET {foreign_field}='other' WHERE id=%s",
            (history,),
        )
    else:
        original = await db.fetch_one(
            "SELECT meta FROM swe_cron_executions WHERE id=%s",
            (history,),
        )
        meta = json.loads(original["meta"])
        meta[foreign_field] = "other"
        await db.execute(
            "UPDATE swe_cron_executions SET meta=%s WHERE id=%s",
            (json.dumps(meta), history),
        )
    assert await ExecutionSettlement(db).tick() == 0


@pytest.mark.asyncio
async def test_two_settlers_observing_the_same_fact_only_commit_once(
    execution_setup,
):
    import asyncio
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    db, _, _ = execution_setup
    row, _ = await completed(execution_setup, "success")
    fetched, both_read = 0, asyncio.Event()
    fetch = db.fetch_all

    async def simultaneous_candidates(query, params=None):
        nonlocal fetched
        rows = await fetch(query, params)
        if query.startswith("SELECT r.execution_id,r.queue_order"):
            fetched += 1
            if fetched == 2:
                both_read.set()
            await both_read.wait()
        return rows

    db.fetch_all = simultaneous_candidates
    results = await asyncio.gather(
        ExecutionSettlement(db).tick(),
        ExecutionSettlement(db).tick(),
    )
    assert sorted(results) == [0, 1]
    current = await db.fetch_one(
        "SELECT version,settled_status FROM swe_cron_execution_runs WHERE execution_id=%s",
        (row["execution_id"],),
    )
    assert current["version"] == 2
    assert current["settled_status"] == "success"


@pytest.mark.asyncio
async def test_primary_error_settles_without_fabricating_async_success(
    execution_setup,
):
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    db, _, _ = execution_setup
    _, history = await completed(execution_setup, status="error")
    assert await ExecutionSettlement(db).tick() == 1
    current = await db.fetch_one(
        "SELECT * FROM swe_cron_executions WHERE id=%s",
        (history,),
    )
    assert current["async_status"] is None


@pytest.mark.asyncio
async def test_async_pending_keeps_the_shared_model_slot(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron import capacity_claim
    from scheduler.app.services.cron.managed.operations import utc_now
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )
    from tests.unit.scheduler.test_managed_cron_capacity import (
        Cursor,
        configure_pool,
    )

    db, _, _ = execution_setup
    _, history = await completed(execution_setup)
    now = utc_now()
    configure_pool(db, now)
    monkeypatch.setattr(capacity_claim, "MANAGED_CAPACITY_ACCOUNTING", True)
    policy = {
        "strategy_id": "default",
        "effective_workers": 2,
        "min_workers": 1,
        "max_workers": 2,
    }

    async def available():
        return await capacity_claim.available_capacity(
            Cursor(db),
            ("bank", "default", "default"),
            "worker",
            now,
            policy,
        )

    assert await available() == 1
    await db.execute(
        "UPDATE swe_cron_executions SET async_status='success' WHERE id=%s",
        (history,),
    )
    assert await ExecutionSettlement(db).tick() == 1
    assert await available() == 2


@pytest.mark.asyncio
async def test_delete_resources_wait_for_async_subtasks(execution_setup):
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    db, caller, management = execution_setup
    row, history = await completed(execution_setup)
    operation = await management.delete(
        caller,
        row["job_id"],
        {"request_id": "delete-async", "expected_definition_version": 1},
    )
    assert operation["stage"] == "cleanup_waiting"
    assert operation["status"] == "pending"
    await db.execute(
        "UPDATE swe_cron_executions SET async_status='success' WHERE id=%s",
        (history,),
    )
    await ExecutionSettlement(db).tick()
    finished = await management.retry(
        caller,
        row["job_id"],
        operation["operation_id"],
    )
    assert finished["status"] == "completed"


@pytest.mark.asyncio
async def test_invalid_history_does_not_starve_later_terminal_work(
    execution_setup,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    db, caller, _ = execution_setup
    first, history = await completed(execution_setup, "success")
    await db.execute(
        "UPDATE swe_cron_executions SET tenant_id='foreign' WHERE id=%s",
        (history,),
    )
    second, _ = await ExecutionStore(db).enqueue(
        caller,
        first["job_id"],
        ExecutionRequest(
            "ordinary",
            "next-period",
            scheduled_at=datetime.now(timezone.utc),
        ),
    )
    await db.execute(
        "UPDATE swe_cron_execution_runs SET status='error' WHERE execution_id=%s",
        (second["execution_id"],),
    )
    service = ExecutionSettlement(db, limit=1)
    assert await service.tick() == 0
    assert await service.tick() == 1
    assert (
        await db.fetch_one(
            "SELECT settled_status FROM swe_cron_execution_runs WHERE execution_id=%s",
            (first["execution_id"],),
        )
    )["settled_status"] is None


@pytest.mark.asyncio
async def test_same_job_waits_for_async_result_in_query_and_atomic_claim(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from scheduler.app.services.cron.managed.operations import utc_now
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )
    from scheduler.app.services.cron.managed.work_query import ManagedWorkQuery
    from tests.unit.scheduler.test_managed_cron_capacity_dispatch import (
        dispatcher,
    )

    db, caller, _ = execution_setup
    first, history = await completed(execution_setup)
    second, _ = await ExecutionStore(db).enqueue(
        caller,
        first["job_id"],
        ExecutionRequest(
            "ordinary",
            "next-period",
            scheduled_at=datetime.now(timezone.utc),
        ),
    )
    now = [utc_now()]
    service = dispatcher(db, now, monkeypatch)
    assert await ManagedWorkQuery(db, 20).rows(running=False) == []
    assert await service._claim(second, True) is None
    await db.execute(
        "UPDATE swe_cron_executions SET async_status='success' WHERE id=%s",
        (history,),
    )
    await ExecutionSettlement(db).tick()
    assert await service._claim(second, True)
