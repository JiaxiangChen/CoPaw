# -*- coding: utf-8 -*-
"""批成员按独立回执归属，普通与批触发不复用，关联原子落库。"""

from datetime import datetime, timezone

import pytest

from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    create_source,
    body,
)


@pytest.mark.asyncio
async def test_batch_prefire_makes_all_members_ready_without_individual_scatter(
    callback_setup,
    monkeypatch,
):
    import json
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )
    from tests.unit.scheduler.test_managed_cron_distribution import payload

    db, caller, management = callback_setup
    created = await create_source(callback_setup, "batch_enabled")
    task = await BroadcastStore(db).submit(
        caller,
        created["job_id"],
        payload(),
    )
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    stored = await db.fetch_one(
        "SELECT payload_json FROM swe_cron_managed_broadcast_tasks WHERE task_id=%s",
        (task["task_id"],),
    )
    timer = json.loads(stored["payload_json"])["timer_sync"]
    assert timer["advance_minutes"] == 120
    assert timer["spec"]["schedule"]["cron"] == "0 7 * * *"
    now = datetime(2026, 10, 15, 23, tzinfo=timezone.utc)
    callback = {
        **body(db, created, kind="batch"),
        "operation_id": task["task_id"],
    }
    accepted = await ManagedSchedulingService(db, clock=lambda: now).accept(
        caller,
        callback,
        "batch",
    )
    rows = await db.fetch_all(
        "SELECT not_before_at, scheduled_fire_at, dispatch_meta_json FROM swe_cron_execution_runs WHERE kind='batch'",
    )
    assert len(rows) == 3
    assert {row["not_before_at"] for row in rows} == {"2026-10-15 23:00:00"}
    assert {row["scheduled_fire_at"] for row in rows} == {
        "2026-10-16 01:00:00",
    }
    assert all(
        json.loads(row["dispatch_meta_json"])["parent_scheduled_fire_at"]
        == "2026-10-16T01:00:00+00:00"
        for row in rows
    )
    from scheduler.app.services.cron.managed import work_query

    monkeypatch.setattr(
        work_query,
        "utc_now",
        lambda: now.replace(tzinfo=None),
    )
    db.conn.create_function("JSON_UNQUOTE", 1, lambda value: value)
    ready = await work_query.ManagedWorkQuery(db, 10).rows(running=False)
    assert len(ready) == accepted["enqueued_intents"] == 3


@pytest.mark.asyncio
async def test_real_batch_callback_persists_members_in_its_acceptance_transaction(
    callback_setup,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, _ = callback_setup
    created = await create_source(callback_setup, "batch_enabled")
    service = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    command = body(db, created, kind="batch")
    first = await service.accept(caller, command, "batch")
    members = await db.fetch_all(
        "SELECT * FROM swe_cron_managed_batch_members WHERE batch_id=%s",
        (first["batch_id"],),
    )
    assert len(members) == 1
    assert (
        members[0]["member_id"]
        == members[0]["initial_execution_id"]
        == members[0]["current_execution_id"]
    )
    assert members[0]["business_attempt"] == 1
    assert members[0]["member_role"] == "parent"
    assert members[0]["source_id"] == caller.source_id
    another = await service.accept(caller, command, "batch")
    assert not another["reused"]
    assert another["batch_id"] != first["batch_id"]
    assert (
        len(await db.fetch_all("SELECT * FROM swe_cron_managed_batch_members"))
        == 2
    )


@pytest.mark.asyncio
async def test_batch_members_do_not_reuse_an_independent_ordinary_arrival(
    callback_setup,
):
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, management = callback_setup
    created = await create_source(callback_setup)
    scheduling = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    original = await scheduling.accept(caller, body(db, created), "ordinary")
    modes = DispatchStateService(db, management.platform)
    change = await modes.set(
        caller,
        created["job_id"],
        {
            "request_id": "batch-mode",
            "state": "batch_enabled",
            "expected_version": 1,
        },
    )
    await modes.process(change["operation_id"])
    command = {
        **body(db, created, kind="batch"),
        "operation_id": change["operation_id"],
    }
    result = await scheduling.accept(caller, command, "batch")
    row = await db.fetch_one(
        "SELECT * FROM swe_cron_managed_batch_members WHERE batch_id=%s",
        (result["batch_id"],),
    )
    assert row is not None
    assert row["current_execution_id"] != original["execution_id"]
    runs = await db.fetch_all("SELECT kind FROM swe_cron_execution_runs")
    assert len(runs) == 2
    assert {run["kind"] for run in runs} == {"ordinary", "batch"}


@pytest.mark.asyncio
async def test_member_failure_rolls_back_receipt_and_enqueued_work(
    callback_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, _ = callback_setup
    created = await create_source(callback_setup, "batch_enabled")
    original = db.execute

    async def fail_member_write(query, params=None):
        if query.startswith("INSERT INTO `swe_cron_managed_batch_members`"):
            raise RuntimeError("injected_relation_failure")
        return await original(query, params)

    monkeypatch.setattr(db, "execute", fail_member_write)
    service = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(RuntimeError, match="injected_relation_failure"):
        await service.accept(caller, body(db, created, kind="batch"), "batch")
    assert not await db.fetch_all(
        "SELECT * FROM swe_cron_managed_trigger_receipts",
    )
    assert not await db.fetch_all("SELECT * FROM swe_cron_execution_runs")
