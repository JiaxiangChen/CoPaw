# -*- coding: utf-8 -*-
"""执行事件必须与共库事实原子提交，重复受理和结果不追加假历史。"""

import json

import pytest

from tests.unit.scheduler.test_managed_cron_executions import (
    execution_setup,
    enqueue,
)


def key(row):
    return tuple(
        row[name] for name in ("tenant_id", "source_id", "agent_id", "job_id")
    )


async def started_run(execution_setup):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from swe.app.crons.managed_results import ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    claimed = await ExecutionDispatcher(db, None)._claim(row, True)
    writer = ExecutionWriter(db)
    await writer.accept(
        row["execution_id"],
        key(row),
        dispatch_token=claimed["dispatch_owner"],
    )
    await writer.start(row["execution_id"], key(row), "owner")
    return row, writer


@pytest.mark.asyncio
async def test_scheduler_and_swe_lifecycle_events_are_ordered_and_idempotent(
    execution_setup,
):
    from swe.app.crons.managed_results import ExecutionOutcome
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    db = execution_setup[0]
    row, writer = await started_run(execution_setup)
    await writer.accept(row["execution_id"], key(row))
    assert await writer.complete(
        row["execution_id"],
        key(row),
        "owner",
        ExecutionOutcome("success"),
    )
    assert not await writer.complete(
        row["execution_id"],
        key(row),
        "owner",
        ExecutionOutcome("success"),
    )
    db.conn.execute("UPDATE swe_cron_executions SET async_status='success'")
    assert await ExecutionSettlement(db).tick() == 1
    assert await ExecutionSettlement(db).tick() == 0
    events = await db.fetch_all(
        "SELECT * FROM swe_cron_managed_dispatch_events ORDER BY event_order",
    )
    assert [event["event_type"] for event in events] == [
        "queued",
        "dispatching",
        "accepted",
        "running",
        "success",
        "settled",
    ]
    assert all(
        event["execution_id"] == row["execution_id"] for event in events
    )
    assert len({event["event_key"] for event in events}) == 6


@pytest.mark.asyncio
async def test_result_event_failure_rolls_back_primary_history_and_state(
    execution_setup,
    monkeypatch,
):
    from swe.app.crons.managed_results import ExecutionOutcome

    db = execution_setup[0]
    row, writer = await started_run(execution_setup)
    original = db.execute

    async def unavailable(query, params=None):
        if "INSERT INTO swe_cron_managed_dispatch_events" in query:
            raise RuntimeError("event_store_unavailable")
        return await original(query, params)

    monkeypatch.setattr(db, "execute", unavailable)
    with pytest.raises(RuntimeError, match="event_store_unavailable"):
        await writer.complete(
            row["execution_id"],
            key(row),
            "owner",
            ExecutionOutcome("success"),
        )
    assert (await writer.get(row["execution_id"], key(row)))[
        "status"
    ] == "running"
    assert not await db.fetch_all("SELECT * FROM swe_cron_executions")
    monkeypatch.setattr(db, "execute", original)
    assert await writer.complete(
        row["execution_id"],
        key(row),
        "owner",
        ExecutionOutcome("success"),
    )
    assert len(await db.fetch_all("SELECT * FROM swe_cron_executions")) == 1


@pytest.mark.asyncio
async def test_live_error_classification_persists_without_raw_error(
    execution_setup,
):
    from swe.app.crons.managed_execution import _failure_outcome

    db = execution_setup[0]
    row, writer = await started_run(execution_setup)
    outcome = _failure_outcome(
        RuntimeError("rate limit secret=private-sensitive-value"),
    )
    await writer.complete(row["execution_id"], key(row), "owner", outcome)
    stored = await writer.get(row["execution_id"], key(row))
    assert (
        json.loads(stored["result_json"])["failure_type"] == "model_rate_limit"
    )
    event = await db.fetch_one(
        "SELECT details_json FROM swe_cron_managed_dispatch_events WHERE event_type='error'",
    )
    assert (
        json.loads(event["details_json"])["failure_type"] == "model_rate_limit"
    )
    assert "private-sensitive-value" not in event["details_json"]
    assert "private-sensitive-value" not in stored["result_json"]
