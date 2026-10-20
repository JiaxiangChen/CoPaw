# -*- coding: utf-8 -*-
"""验证数据库控制不覆盖任务正文、不重置在途执行结果。"""

import json

import pytest

from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)


def state(db):
    return dict(
        db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone(),
    )


def version(db):
    return state(db)["management_version"]


async def success(db, row):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter
    from tests.unit.app.test_managed_cron_execution import Engine, key

    await ManagedExecutionService(ExecutionWriter(db), Engine()).execute(
        row["execution_id"],
        key(row),
    )


@pytest.mark.asyncio
async def test_disable_is_independent_from_source_content_and_batch_entrance(
    execution_setup,
):
    from scheduler.app.services.cron.managed.controls import ControlService

    db, caller, management = execution_setup
    row, _ = await enqueue(execution_setup)
    db.conn.execute(
        "UPDATE swe_cron_job_states SET dispatch_state='batch_enabled'",
    )
    before = list(management.platform.calls)
    result = await ControlService(db, management.platform).set_enabled(
        caller,
        row["job_id"],
        {
            "request_id": "disable-1",
            "enabled": False,
            "expected_management_version": version(db),
        },
    )
    assert result["status"] == "completed"
    definition = db.conn.execute("SELECT * FROM swe_cron_jobs_v2").fetchone()
    assert not definition["enabled"]
    assert definition["definition_version"] == 1
    assert state(db)["pause_reason"] == "manual"
    assert state(db)["dispatch_state"] == "batch_enabled"
    assert management.platform.calls == before


@pytest.mark.asyncio
async def test_resume_clears_existing_unread_only_once_even_after_retry(
    execution_setup,
):
    from scheduler.app.services.cron.managed.controls import ControlService

    db, caller, management = execution_setup
    first, _ = await enqueue(execution_setup)
    await success(db, first)
    controls = ControlService(db, management.platform)
    await controls.set_enabled(
        caller,
        first["job_id"],
        {
            "request_id": "disable",
            "enabled": False,
            "expected_management_version": version(db),
        },
    )
    resume_body = {
        "request_id": "resume",
        "enabled": True,
        "expected_management_version": version(db),
    }
    resumed = await controls.set_enabled(caller, first["job_id"], resume_body)
    assert resumed["status"] == "completed"
    assert state(db)["unread_execution_count"] == 0
    second, _ = await enqueue(execution_setup, trigger="period-2")
    await success(db, second)
    duplicate = await controls.set_enabled(
        caller,
        first["job_id"],
        resume_body,
    )
    assert duplicate["operation_id"] == resumed["operation_id"]
    assert state(db)["unread_execution_count"] == 1
    histories = db.conn.execute(
        "SELECT is_read FROM swe_cron_executions ORDER BY id",
    ).fetchall()
    assert [r["is_read"] for r in histories] == [1, 0]


@pytest.mark.asyncio
async def test_mark_read_does_not_resume_and_duplicate_does_not_clear_new_result(
    execution_setup,
):
    from scheduler.app.services.cron.managed.controls import ControlService

    db, caller, management = execution_setup
    first, _ = await enqueue(execution_setup)
    await success(db, first)
    db.conn.execute(
        "UPDATE swe_cron_job_states SET pause_reason='auto_unread_threshold'",
    )
    controls = ControlService(db, management.platform)
    result = await controls.mark_read(
        caller,
        first["job_id"],
        {"request_id": "read-1"},
    )
    assert result["status"] == "completed"
    assert state(db)["pause_reason"] == "auto_unread_threshold"
    assert state(db)["unread_execution_count"] == 0
    db.conn.execute("UPDATE swe_cron_job_states SET unread_execution_count=2")
    await controls.mark_read(caller, first["job_id"], {"request_id": "read-1"})
    assert state(db)["unread_execution_count"] == 2


@pytest.mark.asyncio
async def test_platform_failure_keeps_db_enable_fact_and_does_not_reapply_read(
    execution_setup,
):
    from scheduler.app.services.cron.managed.controls import ControlService

    db, caller, management = execution_setup
    row, _ = await enqueue(execution_setup)
    controls = ControlService(db, management.platform)
    management.platform.fail_enable = True
    failed = await controls.set_enabled(
        caller,
        row["job_id"],
        {
            "request_id": "resume-failure",
            "enabled": True,
            "expected_management_version": version(db),
        },
    )
    assert failed["status"] == "failed"
    assert state(db)["management_status"] == "ready"
    await success(db, row)
    management.platform.fail_enable = False
    retried = await controls.retry(
        caller,
        row["job_id"],
        failed["operation_id"],
    )
    assert retried["status"] == "completed"
    assert state(db)["unread_execution_count"] == 1


@pytest.mark.asyncio
async def test_control_version_conflicts_and_same_request_changed_body_rejected(
    execution_setup,
):
    from scheduler.app.services.cron.managed.controls import ControlService
    from scheduler.app.services.cron.managed.operations import (
        ManagementConflict,
    )

    db, caller, management = execution_setup
    row, _ = await enqueue(execution_setup)
    controls = ControlService(db, management.platform)
    body = {
        "request_id": "toggle",
        "enabled": False,
        "expected_management_version": version(db),
    }
    await controls.set_enabled(caller, row["job_id"], body)
    with pytest.raises(ManagementConflict):
        await controls.set_enabled(
            caller,
            row["job_id"],
            {**body, "enabled": True},
        )
    with pytest.raises(ManagementConflict, match="version"):
        await controls.set_enabled(
            caller,
            row["job_id"],
            {**body, "request_id": "toggle-new"},
        )


@pytest.mark.asyncio
async def test_priority_survives_content_update_and_does_not_reorder_frozen_run(
    execution_setup,
):
    from scheduler.app.services.cron.managed.controls import ControlService
    from tests.unit.scheduler.test_managed_cron_management import (
        create_payload,
    )

    db, caller, management = execution_setup
    frozen, _ = await enqueue(execution_setup)
    policy = {"user_ids": ["alice"], "branch_ids": ["110"]}
    controls = ControlService(db, management.platform)
    result = await controls.set_priority(
        caller,
        frozen["job_id"],
        {
            "request_id": "priority",
            "expected_management_version": version(db),
            **policy,
        },
    )
    assert result["status"] == "completed"
    assert (
        "batch_dispatch_priority"
        not in json.loads(frozen["spec_json"])["meta"]
    )
    await management.update(
        caller,
        frozen["job_id"],
        {
            "request_id": "edit-content",
            "expected_definition_version": 1,
            "spec": create_payload(name="changed")["spec"],
        },
    )
    spec = json.loads(
        db.conn.execute("SELECT spec_json FROM swe_cron_jobs_v2").fetchone()[
            0
        ],
    )
    assert spec["meta"]["batch_dispatch_priority"] == policy


@pytest.mark.asyncio
async def test_resume_and_finishing_result_race_preserves_new_unread(
    execution_setup,
    monkeypatch,
):
    import asyncio
    from scheduler.app.services.cron.managed import controls
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter
    from tests.unit.app.test_managed_cron_execution import key

    db, caller, management = execution_setup
    first, _ = await enqueue(execution_setup)
    await success(db, first)
    running, _ = await enqueue(execution_setup, trigger="period-2")
    writer = ExecutionWriter(db)
    await writer.accept(running["execution_id"], key(running))
    await writer.start(running["execution_id"], key(running), "running-owner")
    entered, release = asyncio.Event(), asyncio.Event()
    original = controls._mark_histories_read

    async def pause(*args):
        await original(*args)
        entered.set()
        await release.wait()

    monkeypatch.setattr(controls, "_mark_histories_read", pause)
    resume = asyncio.create_task(
        controls.ControlService(db, management.platform).set_enabled(
            caller,
            first["job_id"],
            {
                "request_id": "resume-race",
                "enabled": True,
                "expected_management_version": version(db),
            },
        ),
    )
    await entered.wait()
    finish = asyncio.create_task(
        writer.complete(
            running["execution_id"],
            key(running),
            "running-owner",
            ExecutionOutcome("success", preview="new result"),
        ),
    )
    release.set()
    await asyncio.gather(resume, finish)
    assert state(db)["unread_execution_count"] == 1
    assert [
        row[0]
        for row in db.conn.execute(
            "SELECT is_read FROM swe_cron_executions ORDER BY id",
        ).fetchall()
    ] == [1, 0]


def test_priority_validation_and_primary_branch_list_match_legacy():
    from scheduler.app.services.cron.managed.priority import (
        PRIMARY_BRANCHES,
        normalize_priority,
    )
    from swe.utils.bbk import BBK_MAP

    assert PRIMARY_BRANCHES == {item["value"] for item in BBK_MAP}
    with pytest.raises(ValueError, match="duplicate"):
        normalize_priority({"user_ids": ["alice", "alice"]})
    with pytest.raises(ValueError, match="primary_branch"):
        normalize_priority({"branch_ids": ["unknown"]})


@pytest.mark.asyncio
async def test_delete_can_supersede_failed_enable_sync(execution_setup):
    from scheduler.app.services.cron.managed.controls import ControlService

    db, caller, management = execution_setup
    row, _ = await enqueue(execution_setup)
    management.platform.fail_enable = True
    failed = await ControlService(db, management.platform).set_enabled(
        caller,
        row["job_id"],
        {
            "request_id": "disable-fails",
            "enabled": False,
            "expected_management_version": version(db),
        },
    )
    assert failed["status"] == "failed"
    management.platform.fail_enable = False
    deleted = await management.delete(
        caller,
        row["job_id"],
        {
            "request_id": "delete-after-control-failure",
            "expected_definition_version": 1,
        },
    )
    assert deleted["status"] == "completed"
    old = await management.store.get(
        caller,
        row["job_id"],
        failed["operation_id"],
    )
    assert old["status"] == "superseded"
