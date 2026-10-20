# -*- coding: utf-8 -*-
"""验证模式切换注册已成功但绑定尚未保存时的恢复及租约围栏。"""

import pytest

from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    create_source,
)
from tests.unit.scheduler.test_managed_cron_distribution import payload


async def batch_group(callback_setup):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )

    db, caller, management = callback_setup
    root = await create_source(callback_setup, "batch_enabled")
    task = await BroadcastStore(db).submit(caller, root["job_id"], payload())
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    service = DispatchStateService(db, management.platform)
    current = await service.get(caller, root["job_id"])
    operation = await service.set(
        caller,
        root["job_id"],
        {
            "request_id": "to-ordinary",
            "state": "ordinary",
            "expected_version": current["version"],
        },
    )
    return service, root, operation


@pytest.mark.asyncio
async def test_old_mode_worker_cannot_save_binding_after_lease_transfer(
    callback_setup,
):
    from scheduler.app.services.cron.managed.operations import LeaseLost

    db, _, _ = callback_setup
    service, root, operation = await batch_group(callback_setup)
    checkpoint = service._item_checkpoint

    async def transfer(parent, child, progress):
        await checkpoint(parent, child, progress)
        if progress.get("external_id"):
            db.conn.execute(
                "UPDATE swe_cron_registration_journal SET lease_owner='new-worker' WHERE operation_id=?",
                (operation["operation_id"],),
            )

    service._item_checkpoint = transfer
    with pytest.raises(LeaseLost):
        await service.process(operation["operation_id"])
    state = db.conn.execute(
        "SELECT ordinary_external_job_id FROM swe_cron_job_states WHERE job_id=?",
        (root["job_id"],),
    ).fetchone()
    assert state[0] is None


@pytest.mark.asyncio
async def test_mode_retry_persists_known_registration_before_enabling(
    callback_setup,
):
    db, caller, management = callback_setup
    service, root, operation = await batch_group(callback_setup)

    async def interrupted(*_args):
        raise ConnectionError("temporary storage failure")

    original = service._save_binding
    service._save_binding = interrupted
    await service.process(operation["operation_id"])
    assert (await service.get(caller, root["job_id"]))[
        "sync_status"
    ] == "failed"
    register_count = management.platform.calls.count("register")
    service._save_binding = original
    await service.retry(caller, root["job_id"], operation["operation_id"])
    state = db.conn.execute(
        "SELECT ordinary_external_job_id FROM swe_cron_job_states WHERE job_id=?",
        (root["job_id"],),
    ).fetchone()
    assert state[0] is not None
    assert management.platform.calls.count("register") == register_count
