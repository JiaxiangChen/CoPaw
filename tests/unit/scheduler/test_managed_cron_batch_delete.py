# -*- coding: utf-8 -*-
"""同步批量删除只处理固定目标，持久保存整批幂等及逐项清理结果。"""

import asyncio
import copy

import pytest

from tests.unit.scheduler.test_managed_cron_dispatch_state import (
    group,
    mode_setup,
)


async def setup_batch(mode_setup):
    from scheduler.app.services.cron.managed.batch_delete import (
        BatchDeleteService,
    )

    db, caller, management = mode_setup
    parent_id = await group(mode_setup)
    items = [
        {
            "job_id": row["job_id"],
            "tenant_id": row["tenant_id"],
            "agent_id": row["agent_id"],
            "expected_definition_version": row["definition_version"],
        }
        for row in db.conn.execute(
            "SELECT * FROM swe_cron_jobs_v2 WHERE parent_job_id=? ORDER BY tenant_id",
            (parent_id,),
        ).fetchall()
    ]
    return (
        BatchDeleteService(db, management),
        caller,
        parent_id,
        {"request_id": "delete-selected", "items": items},
    )


@pytest.mark.asyncio
async def test_batch_delete_persists_exact_list_and_reuses_successes(
    mode_setup,
):
    from scheduler.app.services.cron.managed.operations import (
        ManagementConflict,
    )

    service, caller, parent, payload = await setup_batch(mode_setup)
    result = await service.delete(caller, parent, payload)
    assert result["status"] == "completed"
    assert result["deleted_count"] == result["completed_count"] == 2
    assert all(row["logical_delete_applied"] for row in result["results"])
    cleaned = list(service.management.resources.cleaned)
    assert await service.delete(caller, parent, payload) == result
    assert service.management.resources.cleaned == cleaned
    changed = copy.deepcopy(payload)
    changed["items"][0]["expected_definition_version"] += 1
    with pytest.raises(ManagementConflict, match="content_conflict"):
        await service.delete(caller, parent, changed)
    db = service.db
    assert (
        db.conn.execute(
            "SELECT deleted_at FROM swe_cron_jobs_v2 WHERE job_id=?",
            (parent,),
        ).fetchone()[0]
        is None
    )


@pytest.mark.asyncio
async def test_batch_delete_partial_errors_do_not_leak_unrelated_definition(
    mode_setup,
):
    service, caller, parent, payload = await setup_batch(mode_setup)
    payload["items"][1]["expected_definition_version"] += 1
    payload["items"].append(
        {
            "job_id": parent,
            "tenant_id": "alice",
            "agent_id": "default",
            "expected_definition_version": 1,
        },
    )
    result = await service.delete(caller, parent, payload)
    assert result["status"] == "partial_failed"
    assert result["completed_count"] == 1
    assert result["failed_count"] == 2
    assert result["total_count"] == 3
    assert result["results"][-1]["http_status"] == 404
    assert result["results"][-1]["logical_delete_applied"] is False
    assert result["results"][-1]["operation_id"] is None


@pytest.mark.asyncio
async def test_batch_delete_rejects_active_broadcast_before_any_tombstone(
    mode_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.operations import (
        ManagementConflict,
    )
    from tests.unit.scheduler.test_managed_cron_distribution import (
        payload as broadcast_payload,
    )

    service, caller, parent, payload = await setup_batch(mode_setup)
    await BroadcastStore(service.db).submit(
        caller,
        parent,
        broadcast_payload("another-round"),
    )
    with pytest.raises(ManagementConflict, match="broadcast_in_progress"):
        await service.delete(caller, parent, payload)
    assert not service.db.conn.execute(
        "SELECT job_id FROM swe_cron_jobs_v2 WHERE deleted_at IS NOT NULL",
    ).fetchall()


@pytest.mark.asyncio
async def test_failed_cleanup_keeps_tombstone_and_requires_explicit_child_retry(
    mode_setup,
):
    from dataclasses import replace

    service, caller, parent, payload = await setup_batch(mode_setup)
    payload["items"] = payload["items"][:1]
    service.management.platform.fail_stop = True
    result = await service.delete(caller, parent, payload)
    assert result["status"] == "failed"
    assert result["deleted_count"] == 1
    item = result["results"][0]
    assert item["status"] == "failed"
    service.management.platform.fail_stop = False
    replay = await service.delete(caller, parent, payload)
    assert replay["status"] == "failed"
    child = replace(
        caller,
        tenant_id=item["tenant_id"],
        agent_id=item["agent_id"],
    )
    await service.management.retry(child, item["job_id"], item["operation_id"])
    assert (await service.delete(caller, parent, payload))[
        "status"
    ] == "completed"


@pytest.mark.asyncio
async def test_batch_delete_rejects_empty_duplicate_and_missing_agent(
    mode_setup,
):
    service, caller, parent, payload = await setup_batch(mode_setup)
    for items in (
        [],
        payload["items"] * 2,
        [
            {
                key: value
                for key, value in payload["items"][0].items()
                if key != "agent_id"
            },
        ],
    ):
        with pytest.raises(ValueError):
            await service.delete(caller, parent, {**payload, "items": items})
    assert not service.management.resources.cleaned


@pytest.mark.asyncio
async def test_same_source_distribution_cannot_start_during_synchronous_deletion(
    mode_setup,
):
    from scheduler.app.services.cron.managed.distribution import (
        BroadcastConflict,
        BroadcastStore,
    )
    from tests.unit.scheduler.test_managed_cron_distribution import (
        payload as broadcast_payload,
    )

    service, caller, parent, payload = await setup_batch(mode_setup)
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.management.resources.cleanup

    async def cleanup(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    service.management.resources.cleanup = cleanup
    task = asyncio.create_task(service.delete(caller, parent, payload))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(BroadcastConflict, match="management_in_progress"):
            await BroadcastStore(service.db).submit(
                caller,
                parent,
                broadcast_payload("during-delete"),
            )
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_interrupted_batch_resumes_original_order_without_repeating_cleanup(
    mode_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from tests.unit.scheduler.test_managed_cron_distribution import (
        payload as broadcast_payload,
    )

    service, caller, parent, _ = await setup_batch(mode_setup)
    task = await BroadcastStore(service.db).submit(
        caller,
        parent,
        broadcast_payload(
            "twelve-recipients",
            targets=[{"tenant_id": f"user-{index:02}"} for index in range(12)],
        ),
    )
    await BroadcastProcessor(
        service.db,
        service.management.resources,
        service.management.platform,
    ).process(task["task_id"])
    items = [
        {
            "job_id": row["job_id"],
            "tenant_id": row["tenant_id"],
            "agent_id": row["agent_id"],
            "expected_definition_version": row["definition_version"],
        }
        for row in service.db.conn.execute(
            "SELECT * FROM swe_cron_jobs_v2 WHERE tenant_id LIKE 'user-%' ORDER BY tenant_id",
        ).fetchall()
    ]
    payload = {"request_id": "resume-interrupted", "items": items}
    original = service._one

    async def interrupted(caller, parent_id, plan, cached):
        if plan["item"]["tenant_id"] == "user-11":
            raise ConnectionError("database connection interrupted")
        return await original(caller, parent_id, plan, cached)

    service._one = interrupted
    with pytest.raises(ConnectionError):
        await service.delete(caller, parent, payload)
    assert len(service.management.resources.cleaned) == 11
    service._one = original
    result = await service.delete(caller, parent, payload)
    assert result["completed_count"] == 12
    assert [row["tenant_id"] for row in result["results"]] == [
        row["tenant_id"] for row in items
    ]
    assert len(service.management.resources.cleaned) == 12


@pytest.mark.asyncio
async def test_batch_waits_for_running_child_and_replay_observes_completed_cleanup(
    mode_setup,
):
    from dataclasses import replace
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter
    from tests.unit.scheduler.test_managed_cron_recovery import (
        RecoveryClient,
        worker,
    )

    service, caller, parent, payload = await setup_batch(mode_setup)
    item = payload["items"][0]
    child = replace(caller, tenant_id=item["tenant_id"])
    key = child.job_key(item["job_id"])
    run, _ = await ExecutionStore(service.db).enqueue(
        child,
        item["job_id"],
        ExecutionRequest("manual", "before-delete"),
    )
    writer = ExecutionWriter(service.db)
    await writer.accept(run["execution_id"], key)
    await writer.start(run["execution_id"], key, "runner")
    pending = await service.delete(caller, parent, payload)
    assert pending["status"] == "pending"
    assert pending["pending_count"] == 1
    assert pending["deleted_count"] == 2
    await writer.complete(
        run["execution_id"],
        key,
        "runner",
        ExecutionOutcome("success"),
    )
    service.db.conn.execute(
        "UPDATE swe_cron_executions SET async_status='success' WHERE status='success'",
    )
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    await ExecutionSettlement(service.db).tick()
    await worker(
        service.db,
        service.management,
        RecoveryClient(service.db),
    ).tick()
    result = await service.delete(caller, parent, payload)
    assert result["status"] == "completed"
    assert result["completed_count"] == 2
