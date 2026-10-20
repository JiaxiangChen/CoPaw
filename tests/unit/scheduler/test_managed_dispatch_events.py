# -*- coding: utf-8 -*-
"""独立服务事件契约保持一致，公开字段不能夹带异常原文。"""

import json

import pytest

from tests.unit.scheduler.test_managed_cron_executions import (
    execution_setup,
    enqueue,
)


@pytest.mark.asyncio
async def test_scheduler_swe_event_keys_and_public_field_filter_agree(
    execution_setup,
):
    from scheduler.app.services.cron.managed import (
        dispatch_events as scheduler_events,
    )
    from swe.app.crons import managed_events as swe_events

    _, _, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    details = {
        "authorization": "private-key",
        "dispatch_owner": "private-lease",
        "reason": "password=private-secret",
        "status": "private task body",
    }
    first = scheduler_events.event_values(row, "queued", details)
    second = swe_events.event_values(row, "queued", details)
    assert first == second
    assert "private" not in first["details_json"]
    assert (
        json.loads(first["details_json"])["reason"] == "cron_detail_redacted"
    )


@pytest.mark.asyncio
async def test_duplicate_fact_from_other_service_does_not_append_event(
    execution_setup,
):
    from swe.app.crons.managed_events import append_run_event

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    assert not await append_run_event(db, row, "queued")
    assert (
        len(
            await db.fetch_all(
                "SELECT * FROM swe_cron_managed_dispatch_events",
            ),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_arbitrary_event_type_is_not_persisted(execution_setup):
    from scheduler.app.services.cron.managed.dispatch_events import (
        append_run_event,
    )

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    with pytest.raises(ValueError, match="invalid_cron_event_type"):
        await append_run_event(db, row, "invented-event")
    assert (
        len(
            await db.fetch_all(
                "SELECT * FROM swe_cron_managed_dispatch_events",
            ),
        )
        == 1
    )
