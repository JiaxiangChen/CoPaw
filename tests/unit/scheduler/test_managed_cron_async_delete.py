# -*- coding: utf-8 -*-
"""异步受理不删除，持久后台恢复且进度查询无副作用。"""

import copy
import asyncio
from dataclasses import replace

import pytest

from tests.unit.scheduler.test_managed_cron_batch_delete import setup_batch
from tests.unit.scheduler.test_managed_cron_dispatch_state import mode_setup


@pytest.mark.asyncio
async def test_accept_query_and_recreated_worker_use_fixed_snapshot(
    mode_setup,
):
    from scheduler.app.services.cron.managed.async_batch_delete import (
        AsyncBatchDelete,
    )
    from scheduler.app.services.cron.managed.contracts import (
        ManagedCronNotFound,
    )
    from scheduler.app.services.cron.managed.operations import (
        ManagementConflict,
    )

    sync, caller, parent, payload = await setup_batch(mode_setup)
    api = AsyncBatchDelete(sync.db, sync.management)
    accepted = await api.submit(caller, parent, payload)
    assert accepted["operation_status"] == "pending"
    assert accepted["total_count"] == accepted["pending_count"] == 2
    assert accepted["deleted_count"] == 0
    assert not sync.management.resources.cleaned
    assert await api.get(caller, parent, accepted["operation_id"]) == accepted
    assert (await api.current(caller, parent))["operation_id"] == accepted[
        "operation_id"
    ]
    assert not sync.management.resources.cleaned
    with pytest.raises(ManagedCronNotFound):
        await api.get(
            replace(caller, actor_id="mallory"),
            parent,
            accepted["operation_id"],
        )
    changed = copy.deepcopy(payload)
    changed["items"].pop()
    with pytest.raises(ManagementConflict, match="content_conflict"):
        await api.submit(caller, parent, changed)
    worker = AsyncBatchDelete(sync.db, sync.management)
    await worker.process(accepted["operation_id"])
    finished = await api.get(caller, parent, accepted["operation_id"])
    assert finished["operation_status"] == "completed"
    assert finished["completed_count"] == finished["deleted_count"] == 2
    assert finished["pending_count"] == 0
    assert await api.submit(caller, parent, payload) == finished
    await worker.process(accepted["operation_id"])
    assert len(sync.management.resources.cleaned) == 2


@pytest.mark.asyncio
async def test_background_worker_drains_accepted_deletion(mode_setup):
    from scheduler.app.services.cron.managed.async_batch_delete import (
        AsyncBatchDelete,
    )
    from tests.unit.scheduler.test_managed_cron_recovery import (
        RecoveryClient,
        worker,
    )

    sync, caller, parent, payload = await setup_batch(mode_setup)
    api = AsyncBatchDelete(sync.db, sync.management)
    accepted = await api.submit(caller, parent, payload)
    await worker(sync.db, sync.management, RecoveryClient(sync.db)).tick()
    result = await api.get(caller, parent, accepted["operation_id"])
    assert result["completed_count"] == 2


@pytest.mark.asyncio
async def test_background_failure_requires_original_manual_retry(mode_setup):
    from scheduler.app.services.cron.managed.async_batch_delete import (
        AsyncBatchDelete,
    )

    sync, caller, parent, payload = await setup_batch(mode_setup)
    api = AsyncBatchDelete(sync.db, sync.management)
    accepted = await api.submit(caller, parent, payload)
    original = api.sync._one

    async def interrupted(*_):
        raise ConnectionError("not exposed database credentials")

    api.sync._one = interrupted
    with pytest.raises(ConnectionError):
        await api.process(accepted["operation_id"])
    failed = await api.get(caller, parent, accepted["operation_id"])
    assert failed["operation_status"] == "failed"
    assert failed["error_code"] == "cron_batch_delete_processing_failed"
    assert "credentials" not in str(failed)
    api.sync._one = original
    await api.retry(caller, parent, accepted["operation_id"])
    assert not sync.management.resources.cleaned
    await api.process(accepted["operation_id"])
    assert (await api.get(caller, parent, accepted["operation_id"]))[
        "completed_count"
    ] == 2


@pytest.mark.asyncio
async def test_selected_limit_all_snapshot_and_mode_identity(mode_setup):
    from scheduler.app.services.cron.managed.async_batch_delete import (
        AsyncBatchDelete,
    )
    from scheduler.app.services.cron.managed.operations import (
        ManagementConflict,
    )

    sync, caller, parent, payload = await setup_batch(mode_setup)
    api = AsyncBatchDelete(sync.db, sync.management)
    payload["items"] = [
        {**payload["items"][0], "job_id": f"child-{index}"}
        for index in range(1001)
    ]
    with pytest.raises(ValueError, match="invalid_cron_batch_delete_items"):
        await api.submit(caller, parent, payload)
    accepted = await api.submit(
        caller,
        parent,
        {**payload, "all_children": True},
    )
    assert accepted["pending_count"] == accepted["total_count"] == 1001
    assert not sync.management.resources.cleaned
    with pytest.raises(ManagementConflict):
        await api.submit(
            caller,
            parent,
            {**payload, "items": payload["items"][:1]},
        )


@pytest.mark.asyncio
async def test_worker_cancellation_lease_recovery_never_repeats_completed_items(
    mode_setup,
):
    from scheduler.app.services.cron.managed.async_batch_delete import (
        AsyncBatchDelete,
    )

    sync, caller, parent, payload = await setup_batch(mode_setup)
    api = AsyncBatchDelete(sync.db, sync.management)
    accepted = await api.submit(caller, parent, payload)
    entered = asyncio.Event()
    original = api.sync._one

    async def block_second(actor, parent_id, plan, cached):
        if plan["item"]["job_id"] == payload["items"][1]["job_id"]:
            entered.set()
            await asyncio.Event().wait()
        return await original(actor, parent_id, plan, cached)

    api.sync._one = block_second
    pending = asyncio.create_task(api.process(accepted["operation_id"]))
    await asyncio.wait_for(entered.wait(), 2)
    assert len(sync.management.resources.cleaned) == 1
    other = AsyncBatchDelete(sync.db, sync.management)
    from scheduler.app.services.cron.managed.operations import (
        ManagementConflict,
    )

    with pytest.raises(ManagementConflict, match="batch_delete_in_progress"):
        await other.process(accepted["operation_id"])
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    sync.db.conn.execute(
        "UPDATE swe_cron_registration_journal SET lease_until='2000-01-01' WHERE operation_id=?",
        (accepted["operation_id"],),
    )
    await other.process(accepted["operation_id"])
    assert len(sync.management.resources.cleaned) == 2
    assert (await other.get(caller, parent, accepted["operation_id"]))[
        "completed_count"
    ] == 2
