# -*- coding: utf-8 -*-
"""一个热模型池或同任务并发限制不能挡住后面的可运行工作。"""

from datetime import timedelta, timezone

import pytest

from tests.unit.scheduler.test_managed_cron_capacity import configure_pool
from tests.unit.scheduler.test_managed_cron_capacity_dispatch import (
    RuntimePolicy,
    run_for,
)
from tests.unit.scheduler.test_managed_cron_dispatch_state import mode_setup
from tests.unit.scheduler.test_managed_cron_dispatcher import SweClient


def worker(setup, monkeypatch, *, limit=1):
    from scheduler.app.services.cron import capacity_claim
    from scheduler.app.services.cron.managed.capacity import (
        ManagedCapacityGate,
    )
    from scheduler.app.services.cron.managed.operations import utc_now
    from scheduler.app.services.cron.managed.recovery import (
        ManagedRecoveryWorker,
    )

    db, _, management = setup
    configure_pool(db, utc_now() + timedelta(hours=8))
    monkeypatch.setattr(capacity_claim, "MANAGED_CAPACITY_ACCOUNTING", True)
    client = SweClient(db)
    return (
        ManagedRecoveryWorker(
            db,
            management.resources,
            management.platform,
            client,
            capacity=ManagedCapacityGate(RuntimePolicy()),
            limit=limit,
        ),
        client,
    )


@pytest.mark.asyncio
async def test_full_hot_pool_does_not_starve_later_model_pool(
    mode_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from scheduler.app.services.cron.managed.operations import utc_now
    from tests.unit.scheduler.test_managed_cron_management import (
        create_payload,
    )

    db, caller, management = mode_setup
    first = await run_for(mode_setup, 1)
    created = await management.create(
        caller,
        create_payload(
            "other-model",
            model_slot={"provider_id": "z-provider", "model": "z-model"},
        ),
    )
    second, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest("manual", "other-model-click"),
    )
    runtime, client = worker(mode_setup, monkeypatch)
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_intents VALUES (1,'bank','default','default','dispatched')",
    )
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_intents VALUES (2,'bank','default','default','dispatched')",
    )
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_scope_leases VALUES ('bank','z-provider','z-model','worker',?)",
        ((utc_now() + timedelta(hours=8, minutes=5)).isoformat(" "),),
    )
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_worker_capacity VALUES (2,'bank','z-provider','z-model','default',2,?)",
        (utc_now().isoformat(" "),),
    )
    await runtime._dispatch()
    await runtime._dispatch()
    assert second["execution_id"] in client.calls
    assert first["execution_id"] not in client.calls


@pytest.mark.asyncio
async def test_concurrency_blocked_head_does_not_hide_other_task_in_same_pool(
    mode_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from scheduler.app.services.cron.managed.operations import utc_now
    from swe.app.crons.managed_results import ExecutionWriter

    db, caller, _ = mode_setup
    busy = await run_for(mode_setup, 1)
    await ExecutionWriter(db).accept(
        busy["execution_id"],
        caller.job_key(busy["job_id"]),
    )
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET dispatch_lease_until=?",
        ((utc_now() + timedelta(minutes=5)).isoformat(" "),),
    )
    await ExecutionStore(db).enqueue(
        caller,
        busy["job_id"],
        ExecutionRequest(
            "ordinary",
            "next-period",
            scheduled_at=utc_now().replace(tzinfo=timezone.utc),
        ),
    )
    other = await run_for(mode_setup, 2)
    runtime, client = worker(mode_setup, monkeypatch)
    await runtime._dispatch()
    assert client.calls == [other["execution_id"]]


@pytest.mark.asyncio
async def test_one_wave_resolves_model_policy_once_per_scope(
    mode_setup,
    monkeypatch,
):
    for index in range(3):
        await run_for(mode_setup, index)
    runtime, _ = worker(mode_setup, monkeypatch, limit=3)
    calls = []
    policy = runtime.dispatcher.capacity.runtime
    resolve = policy._resolve_worker_strategy

    async def counted(scope, now):
        calls.append(scope.model_dump())
        return await resolve(scope, now)

    monkeypatch.setattr(policy, "_resolve_worker_strategy", counted)
    await runtime._dispatch()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_accepted_work_is_retried_even_when_its_pool_is_full(
    mode_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.operations import utc_now
    from swe.app.crons.managed_results import ExecutionWriter

    db, caller, _ = mode_setup
    row = await run_for(mode_setup, 1)
    await ExecutionWriter(db).accept(
        row["execution_id"],
        caller.job_key(row["job_id"]),
    )
    runtime, client = worker(mode_setup, monkeypatch)
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_intents VALUES (1,'bank','default','default','dispatched')",
    )
    await runtime._dispatch()
    assert client.calls == [row["execution_id"]]


@pytest.mark.asyncio
async def test_wave_permit_cannot_be_reused_for_another_model_scope(
    mode_setup,
    monkeypatch,
):
    from dataclasses import replace

    row = await run_for(mode_setup, 1)
    runtime, client = worker(mode_setup, monkeypatch)
    permit = await runtime.dispatcher.capacity.prepare(row)
    with pytest.raises(ValueError, match="scope_conflict"):
        await runtime.dispatcher.dispatch_registered(
            row["execution_id"],
            capacity_permit=replace(permit, scope=("bank", "other", "other")),
        )
    assert not client.calls


@pytest.mark.asyncio
async def test_invalid_policy_in_one_scope_does_not_abort_other_scopes(
    mode_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from scheduler.app.services.cron.managed.operations import utc_now
    from tests.unit.scheduler.test_managed_cron_management import (
        create_payload,
    )

    db, caller, management = mode_setup
    await run_for(mode_setup, 1)
    created = await management.create(
        caller,
        create_payload(
            "healthy-model",
            model_slot={"provider_id": "z-provider", "model": "z-model"},
        ),
    )
    healthy, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest("manual", "healthy-click"),
    )
    runtime, client = worker(mode_setup, monkeypatch, limit=2)
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_scope_leases VALUES ('bank','z-provider','z-model','worker',?)",
        ((utc_now() + timedelta(hours=8, minutes=5)).isoformat(" "),),
    )
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_worker_capacity VALUES (2,'bank','z-provider','z-model','default',2,?)",
        (utc_now().isoformat(" "),),
    )
    policy = runtime.dispatcher.capacity.runtime
    resolve = policy._resolve_worker_strategy

    async def invalid_first(scope, now):
        if scope.provider_id == "default":
            raise ValueError("invalid policy")
        return await resolve(scope, now)

    monkeypatch.setattr(policy, "_resolve_worker_strategy", invalid_first)
    await runtime._dispatch()
    assert client.calls == [healthy["execution_id"]]
