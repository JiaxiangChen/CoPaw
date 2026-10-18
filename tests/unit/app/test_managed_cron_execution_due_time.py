# -*- coding: utf-8 -*-
"""验证已经受理的提前批工作不会提前运行，且停用水位仍可先行作废它。"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.app.test_managed_cron_execution import Engine, key
from tests.unit.scheduler.test_managed_cron_executions import execution_setup
from tests.unit.scheduler.test_managed_cron_management import create_payload


@pytest.mark.asyncio
async def test_swe_does_not_run_a_future_item_even_when_receiving_early_rpc(
    execution_setup,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db, caller, management = execution_setup
    created = await management.create(caller, create_payload())
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    row, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest(
            "batch",
            "future-period",
            future,
            not_before_at=future,
        ),
    )
    engine = Engine()
    service = ManagedExecutionService(ExecutionWriter(db), engine)
    await service.execute(row["execution_id"], key(row))
    assert not engine.calls
    assert (
        db.conn.execute(
            "SELECT status FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == "accepted"
    )
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET not_before_at='2000-01-01 00:00:00'",
    )
    await service.execute(row["execution_id"], key(row))
    assert len(engine.calls) == 1
