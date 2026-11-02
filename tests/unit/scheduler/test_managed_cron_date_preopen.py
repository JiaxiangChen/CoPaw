# -*- coding: utf-8 -*-
"""23 点预开次日，不提前关闭今天，实际执行许可仍使用当天日期。"""

from datetime import datetime

import pytest

from scheduler.app.services.cron.managed.date_policy import BEIJING
from scheduler.app.services.cron.managed.date_ranges import (
    DateMaintenanceService,
)
from tests.unit.scheduler.test_managed_cron_date_ranges import configure
from tests.unit.scheduler.test_managed_cron_dispatch_state import (
    group,
    mode_setup,
)
from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    create_source,
    body,
)


def freeze_date_policy(monkeypatch, instant):
    from scheduler.app.services.cron.managed import date_policy, date_ranges

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
            )

    monkeypatch.setattr(date_policy, "datetime", FrozenDatetime)
    monkeypatch.setattr(date_ranges, "datetime", FrozenDatetime)


async def set_batch_mode(setup, root, target):
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )

    db, caller, management = setup
    service = DispatchStateService(db, management.platform)
    current = await service.get(caller, root)
    operation = await service.set(
        caller,
        root,
        {
            "request_id": "preopen-mode",
            "state": target,
            "expected_version": current["version"],
        },
    )
    await service.process(operation["operation_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start,end,enabled",
    [
        ("2026-11-03", "2026-11-10", True),
        ("2026-11-01", "2026-11-02", True),
        ("2026-11-01", "2026-11-01", False),
        ("2026-11-04", "2026-11-10", False),
    ],
)
async def test_23h_maintenance_opens_tomorrow_and_retains_today(
    mode_setup,
    monkeypatch,
    start,
    end,
    enabled,
):
    instant = datetime(2026, 11, 2, 23, tzinfo=BEIJING)
    freeze_date_policy(monkeypatch, instant)
    db, caller, management = mode_setup
    root = await group(mode_setup)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": start, "end_date": end},
    )
    management.platform.trace.clear()
    result = await DateMaintenanceService(db, management.platform).apply(
        "bank",
    )
    assert result["failed_count"] == 0
    assert result["evaluated_date"] == "2026-11-02"
    assert management.platform.trace
    assert all(value is enabled for _, value in management.platform.trace)
    assert all(
        row[0]
        for row in db.conn.execute("SELECT enabled FROM swe_cron_jobs_v2")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pause_reason,enabled",
    [("manual", 0), ("unread_auto", 1)],
)
async def test_preopen_preserves_individual_pause_intent(
    mode_setup,
    monkeypatch,
    pause_reason,
    enabled,
):
    freeze_date_policy(monkeypatch, datetime(2026, 11, 2, 23, tzinfo=BEIJING))
    db, caller, management = mode_setup
    root = await group(mode_setup)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-10"},
    )
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET enabled=? WHERE job_id=?",
        (enabled, root),
    )
    db.conn.execute(
        "UPDATE swe_cron_job_states SET pause_reason=? WHERE job_id=?",
        (pause_reason, root),
    )
    external = db.conn.execute(
        "SELECT ordinary_external_job_id FROM swe_cron_job_states WHERE job_id=?",
        (root,),
    ).fetchone()[0]
    management.platform.trace.clear()
    result = await DateMaintenanceService(db, management.platform).apply(
        "bank",
    )
    assert result["failed_count"] == 0
    assert (external, False) in management.platform.trace
    assert (
        db.conn.execute(
            "SELECT enabled FROM swe_cron_jobs_v2 WHERE job_id=?",
            (root,),
        ).fetchone()[0]
        == enabled
    )
    assert (
        db.conn.execute(
            "SELECT pause_reason FROM swe_cron_job_states WHERE job_id=?",
            (root,),
        ).fetchone()[0]
        == pause_reason
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,enabled",
    [("batch_enabled", True), ("batch_paused", False)],
)
async def test_preopen_batch_uses_shared_parent_entry_and_retains_mode(
    mode_setup,
    monkeypatch,
    mode,
    enabled,
):
    freeze_date_policy(monkeypatch, datetime(2026, 11, 2, 23, tzinfo=BEIJING))
    db, caller, management = mode_setup
    root = await group(mode_setup)
    await set_batch_mode(mode_setup, root, mode)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-10"},
    )
    external = db.conn.execute(
        "SELECT batch_external_job_id FROM swe_cron_job_states WHERE job_id=?",
        (root,),
    ).fetchone()[0]
    management.platform.trace.clear()
    result = await DateMaintenanceService(db, management.platform).apply(
        "bank",
    )
    assert result["failed_count"] == 0
    assert management.platform.trace == [(external, enabled)]
    assert {
        row[0]
        for row in db.conn.execute(
            "SELECT dispatch_state FROM swe_cron_job_states",
        )
    } == {mode}


async def callback_task(setup, kind):
    db, caller, management = setup
    mode = "ordinary" if kind == "ordinary" else "batch_enabled"
    created = await create_source(setup, mode)
    operation_id = created["operation_id"]
    if kind == "batch":
        from scheduler.app.services.cron.managed.distribution import (
            BroadcastStore,
        )
        from scheduler.app.services.cron.managed.distribution_worker import (
            BroadcastProcessor,
        )
        from tests.unit.scheduler.test_managed_cron_distribution import payload

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
        operation_id = task["task_id"]
    command = body(db, created, kind=kind)
    command["operation_id"] = operation_id
    command.pop("scheduled_fire_at")
    return created["job_id"], command


def platform_boundary(monkeypatch, db, management):
    from scheduler.app.services.cron.managed import platform

    original = management.platform.set_enabled

    async def set_enabled(external_id, enabled):
        assert not db.conn.in_transaction
        await original(external_id, enabled)

    def client(_config):
        assert not db.conn.in_transaction
        return management.platform

    monkeypatch.setattr(management.platform, "set_enabled", set_enabled)
    monkeypatch.setattr(platform, "ManagedPlatformClient", client)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ordinary", "batch"])
async def test_expired_callback_only_skips_without_platform_or_state_maintenance(
    callback_setup,
    monkeypatch,
    kind,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    instant = datetime(2026, 11, 3, 0, tzinfo=BEIJING)
    freeze_date_policy(monkeypatch, instant)
    db, caller, management = callback_setup
    root, command = await callback_task(callback_setup, kind)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-01", "end_date": "2026-11-02"},
    )
    platform_boundary(monkeypatch, db, management)
    management.platform.trace.clear()
    before = dict(
        db.conn.execute(
            "SELECT * FROM swe_cron_job_states WHERE job_id=?",
            (root,),
        ).fetchone(),
    )
    result = await ManagedSchedulingService(db, clock=lambda: instant).accept(
        caller,
        command,
        kind,
    )
    assert result["accepted"] is False
    assert result["reason"] == "job_outside_date_range"
    assert management.platform.trace == []
    assert not db.conn.execute(
        "SELECT * FROM swe_cron_execution_runs",
    ).fetchall()
    state = db.conn.execute(
        "SELECT * FROM swe_cron_job_states WHERE job_id=?",
        (root,),
    ).fetchone()
    assert dict(state) == before
    assert state["dispatch_state"] == (
        "ordinary" if kind == "ordinary" else "batch_enabled"
    )
    assert all(
        row[0]
        for row in db.conn.execute("SELECT enabled FROM swe_cron_jobs_v2")
    )
    receipt = db.conn.execute(
        "SELECT status FROM swe_cron_managed_trigger_receipts",
    ).fetchone()
    assert receipt[0] == "skipped"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "period",
    [
        {"start_date": "2026-11-04", "end_date": "2026-11-10"},
        {"start_date": "2026-11-01", "end_date": "2026-11-02"},
    ],
)
async def test_manual_execution_outside_dates_is_rejected_without_platform_change(
    callback_setup,
    monkeypatch,
    period,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionConflict,
        ExecutionRequest,
        ExecutionStore,
    )

    freeze_date_policy(monkeypatch, datetime(2026, 11, 3, 0, tzinfo=BEIJING))
    db, caller, management = callback_setup
    root, _ = await callback_task(callback_setup, "ordinary")
    await configure(db, caller, management.platform, root, period)
    management.platform.trace.clear()
    with pytest.raises(ExecutionConflict, match="cron_job_outside_date_range"):
        await ExecutionStore(db).enqueue(
            caller,
            root,
            ExecutionRequest("manual", "outside-dates"),
        )
    assert management.platform.trace == []
    assert not db.conn.execute(
        "SELECT * FROM swe_cron_execution_runs",
    ).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ordinary", "batch"])
@pytest.mark.parametrize("started", [False, True])
async def test_callback_before_start_only_skips_and_start_midnight_can_run(
    callback_setup,
    monkeypatch,
    kind,
    started,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    instant = (
        datetime(2026, 11, 3, 0, tzinfo=BEIJING)
        if started
        else datetime(2026, 11, 2, 23, 30, tzinfo=BEIJING)
    )
    freeze_date_policy(monkeypatch, instant)
    db, caller, management = callback_setup
    root, command = await callback_task(callback_setup, kind)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-03"},
    )
    platform_boundary(monkeypatch, db, management)
    management.platform.trace.clear()
    result = await ManagedSchedulingService(db, clock=lambda: instant).accept(
        caller,
        command,
        kind,
    )
    assert result["accepted"] is started
    assert management.platform.trace == []
    count = db.conn.execute(
        "SELECT COUNT(*) FROM swe_cron_execution_runs",
    ).fetchone()[0]
    assert bool(count) is started


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ordinary", "batch"])
async def test_preopened_entry_can_run_at_midnight_with_previous_pause_record(
    callback_setup,
    monkeypatch,
    kind,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    evening = datetime(2026, 11, 2, 23, tzinfo=BEIJING)
    freeze_date_policy(monkeypatch, evening)
    db, caller, management = callback_setup
    root, command = await callback_task(callback_setup, kind)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-03"},
    )
    result = await DateMaintenanceService(db, management.platform).apply(
        "bank",
    )
    assert result["failed_count"] == 0
    assert (
        db.conn.execute(
            "SELECT date_window_paused FROM swe_cron_job_states WHERE job_id=?",
            (root,),
        ).fetchone()[0]
        == 1
    )
    assert (command["external_job_id"], True) in management.platform.trace
    midnight = datetime(2026, 11, 3, 0, tzinfo=BEIJING)
    freeze_date_policy(monkeypatch, midnight)
    management.platform.trace.clear()
    result = await ManagedSchedulingService(db, clock=lambda: midnight).accept(
        caller,
        command,
        kind,
    )
    assert result["accepted"] is True
    assert management.platform.trace == []


@pytest.mark.asyncio
async def test_expired_http_callback_skips_even_when_platform_is_unavailable(
    callback_setup,
    monkeypatch,
    caplog,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from scheduler.app.routers import managed_callbacks
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    instant = datetime(2026, 11, 3, 0, tzinfo=BEIJING)
    freeze_date_policy(monkeypatch, instant)
    db, caller, management = callback_setup
    root, command = await callback_task(callback_setup, "ordinary")
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-01", "end_date": "2026-11-02"},
    )
    platform_boundary(monkeypatch, db, management)
    management.platform.fail_target = command["external_job_id"]
    management.platform.trace.clear()
    service = ManagedSchedulingService(db, clock=lambda: instant)
    app = FastAPI()
    app.include_router(managed_callbacks.router, prefix="/api")
    app.dependency_overrides[managed_callbacks.scheduling_service] = (
        lambda: service
    )
    command.update(
        tenant_id=caller.tenant_id,
        source_id=caller.source_id,
        agent_id=caller.agent_id,
        fromId=caller.actor_id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/scheduler/cron/ordinary/callback",
            json=command,
        )
    assert response.status_code == 202
    assert response.json()["accepted"] is False
    assert response.json()["status"] == "skipped"
    assert management.platform.trace == []
    assert "过期入口关闭失败" not in caplog.text
    assert "private-platform-error" not in caplog.text
    state = db.conn.execute(
        "SELECT date_window_sync_required FROM swe_cron_job_states WHERE job_id=?",
        (root,),
    ).fetchone()
    assert state[0] == 1
    assert not db.conn.execute(
        "SELECT * FROM swe_cron_execution_runs",
    ).fetchall()


@pytest.mark.asyncio
async def test_maintenance_keeps_currently_valid_entry_open(
    mode_setup,
    monkeypatch,
):
    instant = datetime(2026, 11, 2, 23, tzinfo=BEIJING)
    freeze_date_policy(monkeypatch, instant)
    db, caller, management = mode_setup
    root = await group(mode_setup)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-02", "end_date": "2026-11-03"},
    )
    management.platform.trace.clear()
    result = await DateMaintenanceService(db, management.platform).apply(
        "bank",
    )
    assert result["failed_count"] == 0
    assert management.platform.trace
    assert all(enabled for _, enabled in management.platform.trace)


@pytest.mark.asyncio
async def test_expired_batch_child_cannot_close_shared_parent_timer(
    mode_setup,
    monkeypatch,
):
    instant = datetime(2026, 11, 3, 0, tzinfo=BEIJING)
    freeze_date_policy(monkeypatch, instant)
    db, caller, management = mode_setup
    root = await group(mode_setup)
    await set_batch_mode(mode_setup, root, "batch_enabled")
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-10"},
    )
    child = db.conn.execute(
        "SELECT * FROM swe_cron_jobs_v2 WHERE parent_job_id=? LIMIT 1",
        (root,),
    ).fetchone()
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET run_start_date='2026-11-01',run_end_date='2026-11-02' WHERE job_id=?",
        (child["job_id"],),
    )
    management.platform.trace.clear()
    result = await DateMaintenanceService(db, management.platform).apply(
        "bank",
    )
    assert result["failed_count"] == 0
    assert len(management.platform.trace) == 1
    assert management.platform.trace[0][1] is True


@pytest.mark.asyncio
async def test_headerless_maintenance_returns_today_and_next_day(
    mode_setup,
    monkeypatch,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from scheduler.app.routers import managed_cron_date_ranges

    freeze_date_policy(monkeypatch, datetime(2026, 11, 2, 23, tzinfo=BEIJING))
    db, caller, management = mode_setup
    root = await group(mode_setup)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-10"},
    )
    app = FastAPI()
    app.include_router(managed_cron_date_ranges.router, prefix="/api")
    app.dependency_overrides[managed_cron_date_ranges.date_service] = (
        lambda: DateMaintenanceService(db, management.platform)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/scheduler/cron/date-ranges/apply",
            json={"source_id": "bank"},
        )
    assert response.status_code == 200
    assert response.json()["evaluated_date"] == "2026-11-02"
    assert response.json().get("preopen_date") == "2026-11-03"
    assert response.json()["failed_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,enabled",
    [("ordinary", True), ("batch_enabled", True), ("batch_paused", False)],
)
async def test_new_tomorrow_task_uses_same_entry_policy_as_maintenance(
    callback_setup,
    monkeypatch,
    mode,
    enabled,
):
    from tests.unit.scheduler.test_managed_cron_management import (
        create_payload,
    )

    freeze_date_policy(
        monkeypatch,
        datetime(2026, 11, 2, 23, 30, tzinfo=BEIJING),
    )
    db, caller, management = callback_setup
    command = create_payload(
        "late-create",
        run_date_range={"start_date": "2026-11-03", "end_date": "2026-11-10"},
    )
    command["dispatch_state"] = mode
    command["spec"]["schedule"]["cron"] = "0 0 * * *"
    created = await management.create(caller, command)
    state = db.conn.execute(
        "SELECT * FROM swe_cron_job_states WHERE job_id=?",
        (created["job_id"],),
    ).fetchone()
    column = (
        "ordinary_external_job_id"
        if mode == "ordinary"
        else "batch_external_job_id"
    )
    assert management.platform.trace[-1] == (state[column], enabled)
    assert state["date_window_paused"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ordinary", "batch"])
async def test_content_update_does_not_close_preopened_tomorrow_entry(
    callback_setup,
    monkeypatch,
    kind,
):
    import json

    freeze_date_policy(
        monkeypatch,
        datetime(2026, 11, 2, 23, 30, tzinfo=BEIJING),
    )
    db, caller, management = callback_setup
    mode = "ordinary" if kind == "ordinary" else "batch_enabled"
    created = await create_source(callback_setup, mode)
    root = created["job_id"]
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-10"},
    )
    await DateMaintenanceService(db, management.platform).apply("bank")
    row = db.conn.execute(
        "SELECT * FROM swe_cron_jobs_v2 WHERE job_id=?",
        (root,),
    ).fetchone()
    spec = json.loads(row["spec_json"])
    spec["name"] = "updated after preopen"
    updated = await management.update(
        caller,
        root,
        {
            "request_id": "late-update",
            "expected_definition_version": row["definition_version"],
            "spec": spec,
        },
    )
    assert updated["status"] == "completed"
    assert management.platform.trace[-1][1] is True


@pytest.mark.asyncio
async def test_enable_tomorrow_task_does_not_undo_preopen(
    mode_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.controls import ControlService

    freeze_date_policy(
        monkeypatch,
        datetime(2026, 11, 2, 23, 30, tzinfo=BEIJING),
    )
    db, caller, management = mode_setup
    root = await group(mode_setup)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-10"},
    )
    for enabled in (False, True):
        version = db.conn.execute(
            "SELECT management_version FROM swe_cron_job_states WHERE job_id=?",
            (root,),
        ).fetchone()[0]
        await ControlService(db, management.platform).set_enabled(
            caller,
            root,
            {
                "request_id": "late-enable-" + str(enabled),
                "expected_management_version": version,
                "enabled": enabled,
            },
        )
    assert management.platform.trace[-1][1] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["batch_enabled", "ordinary"])
async def test_mode_switch_keeps_valid_tomorrow_entry_open(
    mode_setup,
    monkeypatch,
    target,
):
    freeze_date_policy(
        monkeypatch,
        datetime(2026, 11, 2, 23, 30, tzinfo=BEIJING),
    )
    db, caller, management = mode_setup
    root = await group(mode_setup)
    if target == "ordinary":
        await set_batch_mode(mode_setup, root, "batch_enabled")
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-10"},
    )
    management.platform.trace.clear()
    if target == "ordinary":
        from scheduler.app.services.cron.managed.dispatch_state import (
            DispatchStateService,
        )

        modes = DispatchStateService(db, management.platform)
        current = await modes.get(caller, root)
        accepted = await modes.set(
            caller,
            root,
            {
                "request_id": "late-ordinary",
                "state": target,
                "expected_version": current["version"],
            },
        )
        await modes.process(accepted["operation_id"])
    else:
        await set_batch_mode(mode_setup, root, target)
    column = (
        "ordinary_external_job_id"
        if target == "ordinary"
        else "batch_external_job_id"
    )
    external = db.conn.execute(
        f"SELECT {column} FROM swe_cron_job_states WHERE job_id=?",
        (root,),
    ).fetchone()[0]
    assert (external, True) in management.platform.trace


@pytest.mark.asyncio
async def test_broadcast_timer_sync_keeps_preopened_batch_entry(
    callback_setup,
    monkeypatch,
):
    freeze_date_policy(
        monkeypatch,
        datetime(2026, 11, 2, 23, 30, tzinfo=BEIJING),
    )
    db, caller, management = callback_setup
    created = await create_source(callback_setup, "batch_enabled")
    root = created["job_id"]
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-10"},
    )
    await DateMaintenanceService(db, management.platform).apply("bank")
    management.platform.trace.clear()
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from tests.unit.scheduler.test_managed_cron_distribution import payload

    task = await BroadcastStore(db).submit(caller, root, payload())
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    external = db.conn.execute(
        "SELECT batch_external_job_id FROM swe_cron_job_states WHERE job_id=?",
        (root,),
    ).fetchone()[0]
    assert management.platform.trace[-1] == (external, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ordinary", "batch"])
async def test_callback_on_last_valid_day_is_not_rejected_early(
    callback_setup,
    monkeypatch,
    kind,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    instant = datetime(2026, 11, 3, 23, 59, 59, tzinfo=BEIJING)
    freeze_date_policy(monkeypatch, instant)
    db, caller, management = callback_setup
    root, command = await callback_task(callback_setup, kind)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-03"},
    )
    management.platform.trace.clear()
    result = await ManagedSchedulingService(db, clock=lambda: instant).accept(
        caller,
        command,
        kind,
    )
    assert result["accepted"] is True
    assert management.platform.trace == []
