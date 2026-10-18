# -*- coding: utf-8 -*-
"""人工批重试保留逻辑成员，幂等响应与尝试指针在同一事务提交。"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    create_source,
    body,
)


async def failed_batch(setup, *, category="model_rate_limit"):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )
    from swe.app.crons.managed_results import ExecutionWriter, ExecutionOutcome

    db, caller, _ = setup
    created = await create_source(setup, "batch_enabled")
    response = await ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    ).accept(caller, body(db, created, kind="batch"), "batch")
    run = await db.fetch_one("SELECT * FROM swe_cron_execution_runs")
    claimed = await ExecutionDispatcher(db, None)._claim(run, True)
    writer = ExecutionWriter(db)
    key = caller.job_key(created["job_id"])
    await writer.accept(
        run["execution_id"],
        key,
        dispatch_token=claimed["dispatch_owner"],
    )
    await writer.start(run["execution_id"], key, "failed-owner")
    await writer.complete(
        run["execution_id"],
        key,
        "failed-owner",
        ExecutionOutcome(
            "error",
            error_code="cron_execution_failed",
            execution_meta={"failure_type": category},
        ),
    )
    return replace(caller, is_manager=True), response["batch_id"]


def retry_body(preview, request_id="retry-1", *, stopped=False):
    from scheduler.app.services.cron.managed.batch_retry import (
        ManagedBatchRetryRequest,
    )

    names = (
        "member_id",
        "execution_id",
        "member_version",
        "execution_version",
        "definition_version",
        "business_attempt",
    )
    return ManagedBatchRetryRequest(
        request_id=request_id,
        confirm_stopped=stopped,
        candidates=[
            {name: item[name] for name in names} for item in preview["items"]
        ],
    )


@pytest.mark.asyncio
async def test_manual_business_retry_keeps_member_count_and_same_request_result(
    callback_setup,
):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    service = BatchRetryService(db)
    preview = await service.preview(caller, batch)
    assert preview["counts"] == [
        {"failure_type": "model_rate_limit", "label": "模型限流", "count": 1},
    ]
    command = retry_body(preview)
    first = await service.retry(caller, batch, command)
    second = await service.retry(caller, batch, command)
    assert first["queued"] == 1
    assert second == {**first, "reused": True}
    members = await db.fetch_all(
        "SELECT * FROM swe_cron_managed_batch_members",
    )
    assert len(members) == 1
    assert members[0]["business_attempt"] == 2
    assert (
        members[0]["initial_execution_id"]
        != members[0]["current_execution_id"]
    )
    runs = await db.fetch_all(
        "SELECT * FROM swe_cron_execution_runs ORDER BY queue_order",
    )
    assert len(runs) == 2
    assert runs[0]["status"] == "error"
    assert runs[1]["status"] == "queued"
    assert runs[1]["kind"] == "batch"
    assert runs[1]["scheduled_fire_at"] == runs[0]["scheduled_fire_at"]
    assert runs[1]["actor_id"] == caller.actor_id


@pytest.mark.asyncio
async def test_different_body_under_same_retry_request_is_conflict(
    callback_setup,
):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
        BatchRetryConflict,
    )

    caller, batch = await failed_batch(callback_setup)
    service = BatchRetryService(callback_setup[0])
    command = retry_body(await service.preview(caller, batch))
    await service.retry(caller, batch, command)
    changed = command.model_copy(update={"confirm_stopped": True})
    with pytest.raises(BatchRetryConflict):
        await service.retry(caller, batch, changed)


@pytest.mark.asyncio
async def test_two_managers_only_advance_a_current_failure_once(
    callback_setup,
):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    caller, batch = await failed_batch(callback_setup)
    service = BatchRetryService(callback_setup[0])
    preview = await service.preview(caller, batch)
    first, second = await asyncio.gather(
        service.retry(caller, batch, retry_body(preview, "manager-1")),
        service.retry(
            replace(caller, actor_id="another-manager"),
            batch,
            retry_body(preview, "manager-2"),
        ),
    )
    assert first["queued"] + second["queued"] == 1
    assert first["skipped"] + second["skipped"] == 1
    assert (
        len(
            await callback_setup[0].fetch_all(
                "SELECT * FROM swe_cron_execution_runs",
            ),
        )
        == 2
    )


@pytest.mark.asyncio
async def test_dangerous_failure_requires_stopped_confirmation_without_partial_write(
    callback_setup,
):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    caller, batch = await failed_batch(callback_setup, category="other")
    db = callback_setup[0]
    service = BatchRetryService(db)
    command = retry_body(await service.preview(caller, batch))
    with pytest.raises(ValueError, match="cron_retry_confirmation_required"):
        await service.retry(caller, batch, command)
    assert (
        len(await db.fetch_all("SELECT * FROM swe_cron_execution_runs")) == 1
    )
    assert not await db.fetch_all(
        "SELECT * FROM swe_cron_managed_dispatch_events WHERE event_type='retry_request'",
    )
    assert (
        await service.retry(
            caller,
            batch,
            command.model_copy(update={"confirm_stopped": True}),
        )
    )["queued"] == 1


@pytest.mark.asyncio
async def test_non_manager_and_cross_source_batch_are_rejected(callback_setup):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    caller, batch = await failed_batch(callback_setup)
    service = BatchRetryService(callback_setup[0])
    with pytest.raises(PermissionError):
        await service.preview(replace(caller, is_manager=False), batch)
    with pytest.raises(LookupError):
        await service.preview(replace(caller, source_id="another-bank"), batch)
