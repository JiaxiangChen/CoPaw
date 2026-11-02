# -*- coding: utf-8 -*-
"""整组暂停先落库，平台失败可续办，子任务不能误停共享父入口。"""

from dataclasses import replace

import pytest

from tests.unit.scheduler.test_managed_cron_dispatch_state import (
    group,
    mode_setup,
)


@pytest.mark.asyncio
async def test_group_pause_is_durable_before_platform_calls(mode_setup):
    from scheduler.app.services.cron.managed.pause_group import (
        PauseGroupService,
    )

    db, caller, management = mode_setup
    root = await group(mode_setup)
    trace = list(management.platform.trace)
    service = PauseGroupService(db, management.platform)
    operation = await service.submit(
        caller,
        root,
        {"request_id": "pause-all", "expected_definition_version": 1},
    )
    assert operation["status"] == "pending"
    assert not operation["logical_delete_applied"]
    assert all(
        not row[0]
        for row in db.conn.execute("SELECT enabled FROM swe_cron_jobs_v2")
    )
    assert all(
        row[0] > 0
        for row in db.conn.execute(
            "SELECT queue_invalidation_version FROM swe_cron_job_states",
        )
    )
    assert management.platform.trace == trace
    await service.process(operation["operation_id"])
    assert (await service.get(caller, root, operation["operation_id"]))[
        "status"
    ] == "completed"
    assert all(
        not enabled for _, enabled in management.platform.trace[len(trace) :]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("child_already_paused", [False, True])
async def test_group_pause_has_unique_sync_ids_and_reuses_original_items(
    mode_setup,
    child_already_paused,
):
    from scheduler.app.services.cron.managed.pause_group import (
        PauseGroupService,
    )

    db, caller, management = mode_setup
    root = await group(mode_setup)
    service = PauseGroupService(db, management.platform)
    if child_already_paused:
        child = db.conn.execute(
            "SELECT * FROM swe_cron_jobs_v2 WHERE parent_job_id=? LIMIT 1",
            (root,),
        ).fetchone()
        manager = replace(
            caller,
            tenant_id=child["tenant_id"],
            is_manager=True,
        )
        operation = await service.submit(
            manager,
            child["job_id"],
            {
                "request_id": "prior-child-pause",
                "expected_definition_version": 1,
            },
        )
        await service.process(operation["operation_id"])
    command = {"request_id": "pause-tree", "expected_definition_version": 1}
    operation = await service.submit(caller, root, command)
    query = (
        "SELECT tenant_id,source_id,agent_id,job_id,sync_operation_id "
        "FROM swe_cron_managed_mode_items WHERE operation_id=? ORDER BY index_no"
    )
    items = [
        tuple(row)
        for row in db.conn.execute(query, (operation["operation_id"],))
    ]
    assert len(items) > 1
    assert len({row[-1] for row in items}) == len(items)
    replay = await service.submit(caller, root, command)
    assert replay["operation_id"] == operation["operation_id"]
    assert items == [
        tuple(row)
        for row in db.conn.execute(query, (operation["operation_id"],))
    ]
    assert all(
        not row[0]
        for row in db.conn.execute("SELECT enabled FROM swe_cron_jobs_v2")
    )


@pytest.mark.asyncio
async def test_pause_batch_closes_shared_entry_and_failure_resumes_only_unfinished(
    mode_setup,
):
    from scheduler.app.services.cron.managed.pause_group import (
        PauseGroupService,
    )
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )

    db, caller, management = mode_setup
    root = await group(mode_setup)
    modes = DispatchStateService(db, management.platform)
    accepted = await modes.set(
        caller,
        root,
        {
            "request_id": "batch",
            "state": "batch_enabled",
            "expected_version": 1,
        },
    )
    await modes.process(accepted["operation_id"])
    state = db.conn.execute(
        "SELECT * FROM swe_cron_job_states WHERE job_id=?",
        (root,),
    ).fetchone()
    external = state["batch_external_job_id"]
    service = PauseGroupService(db, management.platform)
    operation = await service.submit(
        caller,
        root,
        {"request_id": "pause", "expected_definition_version": 1},
    )
    assert {
        row[0]
        for row in db.conn.execute(
            "SELECT dispatch_state FROM swe_cron_job_states",
        )
    } == {"batch_paused"}
    query = (
        "SELECT index_no,sync_operation_id FROM swe_cron_managed_mode_items "
        "WHERE operation_id=? ORDER BY index_no"
    )
    sync_ids = [
        tuple(row)
        for row in db.conn.execute(query, (operation["operation_id"],))
    ]
    management.platform.fail_target = external
    await service.process(operation["operation_id"])
    assert (await service.get(caller, root, operation["operation_id"]))[
        "status"
    ] == "failed"
    assert (external, False) in management.platform.trace
    management.platform.fail_target = None
    await service.retry(caller, root, operation["operation_id"])
    await service.process(operation["operation_id"])
    assert (await service.get(caller, root, operation["operation_id"]))[
        "status"
    ] == "completed"
    assert sync_ids == [
        tuple(row)
        for row in db.conn.execute(query, (operation["operation_id"],))
    ]


@pytest.mark.asyncio
async def test_pausing_child_does_not_pause_parent_or_siblings(mode_setup):
    from scheduler.app.services.cron.managed.pause_group import (
        PauseGroupService,
    )

    db, caller, management = mode_setup
    root = await group(mode_setup)
    child = db.conn.execute(
        "SELECT * FROM swe_cron_jobs_v2 WHERE parent_job_id=? LIMIT 1",
        (root,),
    ).fetchone()
    manager = replace(caller, tenant_id=child["tenant_id"], is_manager=True)
    service = PauseGroupService(db, management.platform)
    operation = await service.submit(
        manager,
        child["job_id"],
        {"request_id": "child-pause", "expected_definition_version": 1},
    )
    await service.process(operation["operation_id"])
    assert (
        db.conn.execute(
            "SELECT enabled FROM swe_cron_jobs_v2 WHERE job_id=?",
            (root,),
        ).fetchone()[0]
        == 1
    )
    disabled = db.conn.execute(
        "SELECT job_id FROM swe_cron_jobs_v2 WHERE enabled=0",
    ).fetchall()
    assert [row[0] for row in disabled] == [child["job_id"]]


@pytest.mark.asyncio
async def test_child_cannot_be_enabled_while_parent_pause_is_pending(
    mode_setup,
):
    from scheduler.app.services.cron.managed.pause_group import (
        PauseGroupService,
    )
    from scheduler.app.services.cron.managed.controls import ControlService
    from scheduler.app.services.cron.managed.operations import (
        ManagementConflict,
    )

    db, caller, management = mode_setup
    root = await group(mode_setup)
    await PauseGroupService(db, management.platform).submit(
        caller,
        root,
        {"request_id": "freeze", "expected_definition_version": 1},
    )
    child = db.conn.execute(
        "SELECT * FROM swe_cron_jobs_v2 WHERE parent_job_id=? LIMIT 1",
        (root,),
    ).fetchone()
    state = db.conn.execute(
        "SELECT * FROM swe_cron_job_states WHERE job_id=?",
        (child["job_id"],),
    ).fetchone()
    manager = replace(caller, tenant_id=child["tenant_id"], is_manager=True)
    with pytest.raises(
        ManagementConflict,
        match="cron_parent_pause_in_progress",
    ):
        await ControlService(db, management.platform).set_enabled(
            manager,
            child["job_id"],
            {
                "request_id": "illegal-enable",
                "enabled": True,
                "expected_management_version": state["management_version"],
            },
        )


@pytest.mark.asyncio
async def test_pause_http_uses_delete_request_and_common_operation_query(
    mode_setup,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from scheduler.app.routers import (
        api_router,
        managed_cron_controls,
        managed_cron_management,
    )
    from scheduler.app.services.cron.managed.controls import ControlService
    from scheduler.app.services.cron.managed.pause_group import (
        PauseGroupService,
    )

    db, caller, management = mode_setup
    root = await group(mode_setup)
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    app.dependency_overrides[managed_cron_controls.control_service] = (
        lambda: ControlService(db, management.platform)
    )
    app.dependency_overrides[managed_cron_management.management_database] = (
        lambda: db
    )
    headers = {"X-User-Id": "alice", "X-Source-Id": "bank"}
    path = f"/api/scheduler/cron/jobs/{root}"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        accepted = await client.post(
            path + "/pause",
            headers=headers,
            json={
                "request_id": "pause-http",
                "expected_definition_version": 1,
            },
        )
        assert accepted.status_code == 202
        operation = accepted.json()["operation"]
        assert operation["action"] == "pause_group"
        await PauseGroupService(db, management.platform).process(
            operation["operation_id"],
        )
        status = await client.get(
            path + "/operations/" + operation["operation_id"],
            headers=headers,
        )
        assert status.status_code == 200
        assert status.json()["status"] == "completed"
