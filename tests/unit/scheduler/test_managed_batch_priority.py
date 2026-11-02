# -*- coding: utf-8 -*-
"""模型池优先派发较早批次，但不等待其已运行任务完成。"""

from datetime import datetime, timedelta, timezone

import pytest

from scheduler.app.services.cron.managed.work_query import ManagedWorkQuery
from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    body,
)
from tests.unit.scheduler.test_managed_cron_distribution import payload

SCOPE = {"source_id": "bank", "provider_id": "default", "model_id": "default"}


async def batch(setup, name, second):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, management = setup
    created = await management.create(
        caller,
        {
            "request_id": name,
            "dispatch_state": "batch_enabled",
            "spec": {
                "name": name,
                "task_type": "text",
                "text": "body",
                "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
            },
        },
    )
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
    accepted = await ManagedSchedulingService(
        db,
        clock=lambda: datetime(
            2026,
            10,
            16,
            1,
            0,
            second,
            tzinfo=timezone.utc,
        ),
    ).accept(caller, body(db, created, kind="batch", trigger=name), "batch")
    return [
        row[0]
        for row in db.conn.execute(
            "SELECT current_execution_id FROM swe_cron_managed_batch_members WHERE batch_id=? ORDER BY dispatch_order",
            (accepted["batch_id"],),
        )
    ]


@pytest.mark.asyncio
async def test_earlier_batch_members_precede_later_batch_first_member(
    callback_setup,
):
    db = callback_setup[0]
    first = await batch(callback_setup, "A", 0)
    second = await batch(callback_setup, "B", 1)
    rows = await ManagedWorkQuery(db, 200).rows(running=False, scope=SCOPE)
    assert len(first) > 1
    assert [row["execution_id"] for row in rows] == first + second


@pytest.mark.asyncio
async def test_running_earlier_batch_does_not_block_later_candidates(
    callback_setup,
):
    db = callback_setup[0]
    first = await batch(callback_setup, "A", 0)
    second = await batch(callback_setup, "B", 1)
    for identifier in first:
        db.conn.execute(
            "UPDATE swe_cron_execution_runs SET status='running' WHERE execution_id=?",
            (identifier,),
        )
    rows = await ManagedWorkQuery(db, 200).rows(running=False, scope=SCOPE)
    assert [row["execution_id"] for row in rows] == second


@pytest.mark.asyncio
async def test_retry_not_yet_due_does_not_reserve_candidate_page(
    callback_setup,
):
    db = callback_setup[0]
    first = await batch(callback_setup, "A", 0)
    second = await batch(callback_setup, "B", 1)
    due = (
        (datetime.now(timezone.utc) + timedelta(minutes=5))
        .replace(tzinfo=None)
        .isoformat(" ")
    )
    for identifier in first:
        db.conn.execute(
            "UPDATE swe_cron_execution_runs SET not_before_at=? WHERE execution_id=?",
            (due, identifier),
        )
    rows = await ManagedWorkQuery(db, 200).rows(running=False, scope=SCOPE)
    assert [row["execution_id"] for row in rows] == second


@pytest.mark.asyncio
async def test_urgent_candidate_precedes_normal_batch_order(callback_setup):
    db = callback_setup[0]
    first = await batch(callback_setup, "A", 0)
    second = await batch(callback_setup, "B", 1)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET dispatch_lane='urgent' WHERE execution_id=?",
        (second[-1],),
    )
    rows = await ManagedWorkQuery(db, 200).rows(running=False, scope=SCOPE)
    assert rows[0]["execution_id"] == second[-1]
