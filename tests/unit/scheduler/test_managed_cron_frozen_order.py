# -*- coding: utf-8 -*-
"""入队固定旧规则的批内顺序，后续配置及墙钟变化不能重排。"""

from datetime import datetime, timezone

import pytest

from tests.unit.scheduler.test_managed_cron_callbacks import (
    body,
    callback_setup,
    create_source,
)
from tests.unit.scheduler.test_managed_cron_distribution import payload


async def group(setup, targets=None):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )

    db, caller, management = setup
    root = await create_source(setup, "batch_enabled")
    command = payload(targets=targets)
    command.update(enable_offset=False, offset_window_hours=0)
    task = await BroadcastStore(db).submit(caller, root["job_id"], command)
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    callback = body(db, root, kind="batch")
    callback["operation_id"] = task["task_id"]
    return root, callback


async def priority(setup, job_id, request_id, users, branches=None):
    from scheduler.app.services.cron.managed.controls import ControlService

    db, caller, _ = setup
    version = db.conn.execute(
        "SELECT management_version FROM swe_cron_job_states WHERE job_id=?",
        (job_id,),
    ).fetchone()[0]
    await ControlService(db).set_priority(
        caller,
        job_id,
        {
            "request_id": request_id,
            "expected_management_version": version,
            "user_ids": users,
            "branch_ids": branches or [],
        },
    )


@pytest.mark.asyncio
async def test_priority_survives_clock_change_and_later_configuration_update(
    callback_setup,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )
    from scheduler.app.services.cron.managed.work_query import ManagedWorkQuery

    db, caller, _ = callback_setup
    root, callback = await group(callback_setup)
    await priority(callback_setup, root["job_id"], "priority-carol", ["carol"])
    await ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    ).accept(caller, callback, "batch")
    db.conn.create_function("JSON_UNQUOTE", 1, lambda value: value)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET updated_at='2000-01-01 00:00:00' WHERE tenant_id<>'carol'",
    )
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET updated_at='2099-01-01 00:00:00' WHERE tenant_id='carol'",
    )
    await priority(callback_setup, root["job_id"], "priority-bob", ["bob"])
    first = (await ManagedWorkQuery(db, 1).rows(running=False))[0]
    tenant = db.conn.execute(
        "SELECT tenant_id FROM swe_cron_execution_runs WHERE execution_id=?",
        (first["execution_id"],),
    ).fetchone()[0]
    assert tenant == "carol"


@pytest.mark.asyncio
async def test_existing_heat_rule_breaks_unconfigured_priority_ties(
    callback_setup,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )
    from scheduler.app.services.cron.managed.work_query import ManagedWorkQuery

    db, caller, _ = callback_setup
    root, callback = await group(callback_setup)
    child_id = db.conn.execute(
        "SELECT job_id FROM swe_cron_jobs_v2 WHERE tenant_id='carol'",
    ).fetchone()[0]
    db.conn.execute(
        "INSERT INTO swe_cron_executions (job_id,tenant_id,status,async_status,is_read,end_time,read_at,created_at) VALUES (?,?, 'success','success',1,'2026-10-16 08:00:00','2026-10-16 08:10:00','2026-10-16 08:00:00')",
        (child_id, "carol"),
    )
    await ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    ).accept(caller, callback, "batch")
    db.conn.create_function("JSON_UNQUOTE", 1, lambda value: value)
    first = (await ManagedWorkQuery(db, 1).rows(running=False))[0]
    tenant = db.conn.execute(
        "SELECT tenant_id FROM swe_cron_execution_runs WHERE execution_id=?",
        (first["execution_id"],),
    ).fetchone()[0]
    assert tenant == "carol"


@pytest.mark.asyncio
async def test_user_precedes_branch_and_branch_precedes_heat(callback_setup):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, _ = callback_setup
    root, callback = await group(
        callback_setup,
        targets=[
            {"tenant_id": "bob", "bbk_id": "110"},
            {"tenant_id": "carol", "bbk_id": "100"},
        ],
    )
    await priority(
        callback_setup,
        root["job_id"],
        "user-and-branch",
        ["bob"],
        ["100"],
    )
    await ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    ).accept(caller, callback, "batch")
    assert [
        row[0]
        for row in db.conn.execute(
            "SELECT tenant_id FROM swe_cron_execution_runs ORDER BY dispatch_order,queue_order",
        )
    ] == ["bob", "carol", "alice"]


@pytest.mark.asyncio
async def test_later_priority_only_changes_new_batch_order(callback_setup):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, _ = callback_setup
    root, callback = await group(callback_setup)
    await priority(callback_setup, root["job_id"], "first-priority", ["carol"])
    service = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    first = await service.accept(caller, callback, "batch")
    await priority(callback_setup, root["job_id"], "next-priority", ["bob"])
    replay = await service.accept(caller, callback, "batch")
    assert not replay["reused"]
    assert replay["batch_id"] != first["batch_id"]
    callback.update(
        trigger_id="next-period",
        scheduled_fire_at="2026-10-17T09:00:00+08:00",
    )
    await ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 17, 1, tzinfo=timezone.utc),
    ).accept(caller, callback, "batch")
    first_period = db.conn.execute(
        "SELECT tenant_id FROM swe_cron_execution_runs WHERE json_extract(dispatch_meta_json,'$.batch_id')=? ORDER BY dispatch_order LIMIT 1",
        (first["batch_id"],),
    ).fetchone()[0]
    replay_priority = db.conn.execute(
        "SELECT tenant_id FROM swe_cron_execution_runs WHERE json_extract(dispatch_meta_json,'$.batch_id')=? ORDER BY dispatch_order LIMIT 1",
        (replay["batch_id"],),
    ).fetchone()[0]
    next_period = db.conn.execute(
        "SELECT tenant_id FROM swe_cron_execution_runs WHERE scheduled_fire_at='2026-10-17 01:00:00' ORDER BY dispatch_order LIMIT 1",
    ).fetchone()[0]
    assert first_period == "carol"
    assert replay_priority == "bob"
    assert next_period == "bob"
