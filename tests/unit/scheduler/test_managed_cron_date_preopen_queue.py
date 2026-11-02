# -*- coding: utf-8 -*-
"""预开日期标记滞后时，过期维护仍必须永久使旧队列失效。"""

from datetime import datetime

import pytest

from scheduler.app.services.cron.managed.date_policy import BEIJING
from scheduler.app.services.cron.managed.date_ranges import (
    DateMaintenanceService,
)
from scheduler.app.services.cron.managed.dispatcher import ExecutionDispatcher
from scheduler.app.services.cron.managed.repository import (
    ManagedCronRepository,
)
from scheduler.app.services.cron.managed.scheduling import (
    ManagedSchedulingService,
)
from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup
from tests.unit.scheduler.test_managed_cron_date_preopen import (
    callback_task,
    freeze_date_policy,
)
from tests.unit.scheduler.test_managed_cron_date_ranges import configure


@pytest.mark.asyncio
async def test_expiry_invalidates_old_queue_with_stale_prestart_flag(
    callback_setup,
    monkeypatch,
):
    freeze_date_policy(monkeypatch, datetime(2026, 11, 2, 23, tzinfo=BEIJING))
    db, caller, management = callback_setup
    root, command = await callback_task(callback_setup, "ordinary")
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-03", "end_date": "2026-11-03"},
    )
    await DateMaintenanceService(db, management.platform).apply("bank")
    midnight = datetime(2026, 11, 3, 0, tzinfo=BEIJING)
    freeze_date_policy(monkeypatch, midnight)
    accepted = await ManagedSchedulingService(
        db,
        clock=lambda: midnight,
    ).accept(caller, command, "ordinary")
    run = dict(
        db.conn.execute(
            "SELECT * FROM swe_cron_execution_runs WHERE execution_id=?",
            (accepted["execution_id"],),
        ).fetchone(),
    )
    # 故意漏掉有效日 23 点维护，日期暂停标记仍来自预开当晚。
    expired = datetime(2026, 11, 4, 0, tzinfo=BEIJING)
    freeze_date_policy(monkeypatch, expired)
    assert (
        db.conn.execute(
            "SELECT date_window_paused FROM swe_cron_job_states WHERE job_id=?",
            (root,),
        ).fetchone()[0]
        == 1
    )
    result = await DateMaintenanceService(db, management.platform).apply(
        "bank",
    )
    assert result["failed_count"] == 0
    invalidation = db.conn.execute(
        "SELECT queue_invalidation_version FROM swe_cron_job_states WHERE job_id=?",
        (root,),
    ).fetchone()[0]
    assert invalidation > run["enqueued_state_version"]
    await configure(
        db,
        caller,
        management.platform,
        root,
        {"start_date": "2026-11-04", "end_date": "2026-11-10"},
        request="extend-after-expiry",
    )
    current = await ManagedCronRepository(db)._get_row(
        caller.job_key(root),
        False,
    )
    assert ExecutionDispatcher._invalidated(run, current)
