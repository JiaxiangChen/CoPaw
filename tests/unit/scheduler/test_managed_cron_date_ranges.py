# -*- coding: utf-8 -*-
"""真实父子事务验证日期修改与平台单次维护，不启动内部日期循环。"""

import json

import pytest

from scheduler.app.services.cron.managed.controls import ControlService
from scheduler.app.services.cron.managed.operations import ManagementConflict
from tests.unit.scheduler.test_managed_cron_dispatch_state import (
    group,
    mode_setup,
)
from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup


async def configure(db, caller, platform, job_id, period, request="dates"):
    version = db.conn.execute(
        "SELECT management_version FROM swe_cron_job_states WHERE job_id=?",
        (job_id,),
    ).fetchone()[0]
    return await ControlService(db, platform).set_date_range(
        caller,
        job_id,
        {
            "request_id": request,
            "expected_management_version": version,
            "run_date_range": period,
        },
    )


@pytest.mark.asyncio
async def test_parent_can_set_dates_with_children_without_content_update(
    mode_setup,
):
    db, caller, management = mode_setup
    root = await group(mode_setup)
    before = list(management.platform.trace)
    period = {"start_date": "2099-01-01", "end_date": "2099-01-02"}
    operation = await configure(db, caller, management.platform, root, period)
    assert operation["status"] == "completed"
    rows = db.conn.execute("SELECT * FROM swe_cron_jobs_v2").fetchall()
    assert len(rows) > 1
    for row in rows:
        assert row["run_start_date"] == "2099-01-01"
        assert json.loads(row["spec_json"])["run_date_range"] == period
        assert row["definition_version"] == 1
        assert row["enabled"] == 1
    assert management.platform.trace == before


@pytest.mark.asyncio
async def test_single_maintenance_call_applies_dates_and_clear_preserves_manual_pause(
    mode_setup,
):
    from scheduler.app.services.cron.managed.date_ranges import (
        DateMaintenanceService,
    )

    db, caller, management = mode_setup
    root = await group(mode_setup)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {
            "start_date": "2000-01-01",
            "end_date": "2000-01-02",
        },
    )
    maintenance = DateMaintenanceService(db, management.platform)
    management.platform.trace.clear()
    result = await maintenance.apply("bank")
    assert result["failed_count"] == 0
    assert management.platform.trace
    assert all(not enabled for _, enabled in management.platform.trace)
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET enabled=0 WHERE job_id=?",
        (root,),
    )
    db.conn.execute(
        "UPDATE swe_cron_job_states SET pause_reason='manual' WHERE job_id=?",
        (root,),
    )
    await configure(
        db,
        caller,
        management.platform,
        root,
        None,
        request="clear-dates",
    )
    management.platform.trace.clear()
    result = await maintenance.apply("bank")
    assert result["failed_count"] == 0
    states = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchall()
    assert all(row["date_window_paused"] == 0 for row in states)
    assert all(row["date_window_sync_required"] == 0 for row in states)
    own = next(row for row in states if row["job_id"] == root)
    assert own["pause_reason"] == "manual"
    assert (
        own["ordinary_external_job_id"],
        False,
    ) in management.platform.trace


@pytest.mark.asyncio
async def test_child_date_configuration_cannot_override_inherited_range(
    mode_setup,
):
    from dataclasses import replace

    db, caller, management = mode_setup
    root = await group(mode_setup)
    child = db.conn.execute(
        "SELECT * FROM swe_cron_jobs_v2 WHERE parent_job_id=? LIMIT 1",
        (root,),
    ).fetchone()
    manager = replace(caller, tenant_id=child["tenant_id"], is_manager=True)
    with pytest.raises(
        ManagementConflict,
        match="cron_date_range_requires_source",
    ):
        await configure(
            db,
            manager,
            management.platform,
            child["job_id"],
            None,
        )


@pytest.mark.asyncio
async def test_callback_checks_current_date_columns_not_registration_snapshot(
    callback_setup,
):
    from datetime import datetime, timezone
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )
    from tests.unit.scheduler.test_managed_cron_callbacks import (
        create_source,
        body,
    )

    db, caller, management = callback_setup
    created = await create_source(callback_setup)
    await configure(
        db,
        caller,
        management.platform,
        created["job_id"],
        {"start_date": "2000-01-01", "end_date": "2000-01-02"},
    )
    management.platform.trace.clear()
    result = await ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    ).accept(caller, body(db, created), "ordinary")
    assert result["accepted"] is False
    assert result["status"] == "skipped"
    assert management.platform.trace == []
    assert not db.conn.execute(
        "SELECT * FROM swe_cron_execution_runs",
    ).fetchall()


def test_date_invalidated_work_can_be_settled_before_future_not_before():
    from datetime import datetime, timezone
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )

    run = {"not_before_at": datetime(2099, 1, 1), "enqueued_state_version": 1}
    row = {
        "enabled": True,
        "deleted_at": None,
        "state_pause_reason": None,
        "run_start_date": "2000-01-01",
        "run_end_date": "2000-01-02",
    }
    assert ExecutionDispatcher(
        None,
        None,
        clock=lambda: datetime(2026, 1, 1),
    )._due(run, row)


@pytest.mark.asyncio
async def test_child_full_content_update_cannot_clear_inherited_dates(
    mode_setup,
):
    from scheduler.app.services.cron.managed.definition import normalize_spec
    from dataclasses import replace

    db, caller, management = mode_setup
    root = await group(mode_setup)
    period = {"start_date": "2099-01-01", "end_date": "2099-01-02"}
    await configure(db, caller, management.platform, root, period)
    child = dict(
        db.conn.execute(
            "SELECT * FROM swe_cron_jobs_v2 WHERE parent_job_id=? LIMIT 1",
            (root,),
        ).fetchone(),
    )
    raw = json.loads(child["spec_json"])
    raw["run_date_range"] = None
    manager = replace(caller, tenant_id=child["tenant_id"], is_manager=True)
    with pytest.raises(ValueError, match="cron_date_range_inherited"):
        normalize_spec(raw, manager, child["job_id"], child)


@pytest.mark.asyncio
async def test_date_configuration_waits_for_active_distribution_snapshot(
    mode_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from tests.unit.scheduler.test_managed_cron_distribution import payload

    db, caller, management = mode_setup
    root = await group(mode_setup)
    command = {
        **payload(),
        "request_id": "pending-distribution",
        "targets": [{"tenant_id": "new-user", "agent_id": "default"}],
    }
    await BroadcastStore(db).submit(caller, root, command)
    with pytest.raises(ManagementConflict, match="cron_broadcast_in_progress"):
        await configure(db, caller, management.platform, root, None)


@pytest.mark.asyncio
async def test_maintenance_does_not_clear_dirty_flag_after_management_change(
    mode_setup,
):
    from scheduler.app.services.cron.managed.date_ranges import (
        DateMaintenanceService,
    )

    db, caller, management = mode_setup
    root = await group(mode_setup)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2000-01-01", "end_date": "2000-01-02"},
    )
    original = management.platform.set_enabled
    changed = False

    async def concurrent_change(external_id, enabled):
        nonlocal changed
        if not changed:
            changed = True
            db.conn.execute(
                "UPDATE swe_cron_job_states SET management_version=management_version+1 WHERE job_id=?",
                (root,),
            )
        await original(external_id, enabled)

    management.platform.set_enabled = concurrent_change
    service = DateMaintenanceService(db, management.platform)
    result = await service.apply("bank")
    assert result["failed_count"] == 1
    assert (
        db.conn.execute(
            "SELECT date_window_sync_required FROM swe_cron_job_states WHERE job_id=?",
            (root,),
        ).fetchone()[0]
        == 1
    )
    assert (await service.apply("bank"))["failed_count"] == 0


@pytest.mark.asyncio
async def test_platform_failure_remains_dirty_until_next_manual_apply(
    mode_setup,
):
    from scheduler.app.services.cron.managed.date_ranges import (
        DateMaintenanceService,
    )

    db, caller, management = mode_setup
    root = await group(mode_setup)
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2000-01-01", "end_date": "2000-01-02"},
    )
    external_id = db.conn.execute(
        "SELECT ordinary_external_job_id FROM swe_cron_job_states WHERE job_id=?",
        (root,),
    ).fetchone()[0]
    management.platform.fail_target = external_id
    service = DateMaintenanceService(db, management.platform)
    result = await service.apply("bank")
    assert result["failed_count"] == 1
    assert result["platform_updated_count"] > 0
    assert (
        db.conn.execute(
            "SELECT date_window_sync_required FROM swe_cron_job_states WHERE job_id=?",
            (root,),
        ).fetchone()[0]
        == 1
    )
    management.platform.fail_target = None
    assert (await service.apply("bank"))["failed_count"] == 0
    assert (
        db.conn.execute(
            "SELECT date_window_sync_required FROM swe_cron_job_states WHERE job_id=?",
            (root,),
        ).fetchone()[0]
        == 0
    )
