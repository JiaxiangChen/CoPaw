# -*- coding: utf-8 -*-
"""用真实在途执行验证结果顺序、已读水位和停用恢复的队列边界。"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.unit.scheduler.test_managed_cron_executions import execution_setup
from tests.unit.scheduler.test_managed_cron_management import create_payload


async def setup_job(execution_setup):
    db, caller, management = execution_setup
    created = await management.create(
        caller,
        create_payload(
            runtime={
                "max_concurrency": 2,
                "timeout_seconds": 7200,
                "misfire_grace_seconds": 300,
            },
        ),
    )
    return db, caller, management, created["job_id"]


async def add_run(db, caller, job_id, trigger):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )

    row, _ = await ExecutionStore(db).enqueue(
        caller,
        job_id,
        ExecutionRequest(
            "ordinary",
            trigger,
            datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
        ),
    )
    return row


@pytest.mark.asyncio
async def test_old_snapshot_success_after_content_update_is_still_a_new_unread_result(
    execution_setup,
):
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db, caller, management, job_id = await setup_job(execution_setup)
    row = await add_run(db, caller, job_id, "period-1")
    writer, key = ExecutionWriter(db), caller.job_key(job_id)
    await writer.accept(row["execution_id"], key)
    await writer.start(row["execution_id"], key, "old-worker")
    await management.update(
        caller,
        job_id,
        {
            "request_id": "edit",
            "expected_definition_version": 1,
            "spec": create_payload(name="new-definition")["spec"],
        },
    )
    await writer.complete(
        row["execution_id"],
        key,
        "old-worker",
        ExecutionOutcome("success", preview="old snapshot report"),
    )
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    assert state["unread_execution_count"] == 1
    assert state["latest_scheduled_preview"] == "old snapsh"
    assert (
        db.conn.execute("SELECT name FROM swe_cron_jobs_v2").fetchone()[0]
        == "new-definition"
    )


@pytest.mark.asyncio
async def test_old_completion_never_overwrites_a_later_started_success(
    execution_setup,
):
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db, caller, management, job_id = await setup_job(execution_setup)
    key, writer = caller.job_key(job_id), ExecutionWriter(db)
    first = await add_run(db, caller, job_id, "period-1")
    await writer.accept(first["execution_id"], key)
    await writer.start(first["execution_id"], key, "first")
    await management.update(
        caller,
        job_id,
        {
            "request_id": "edit",
            "expected_definition_version": 1,
            "spec": create_payload(
                name="new",
                runtime={
                    "max_concurrency": 2,
                    "timeout_seconds": 7200,
                    "misfire_grace_seconds": 300,
                },
            )["spec"],
        },
    )
    second = await add_run(db, caller, job_id, "period-2")
    await writer.accept(second["execution_id"], key)
    await writer.start(second["execution_id"], key, "second")
    await writer.complete(
        second["execution_id"],
        key,
        "second",
        ExecutionOutcome("success", preview="new report"),
    )
    await writer.complete(
        first["execution_id"],
        key,
        "first",
        ExecutionOutcome("success", preview="late old"),
    )
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    assert state["latest_scheduled_preview"] == "new report"
    assert state["unread_execution_count"] == 2
    assert state["last_status"] == "success"


@pytest.mark.asyncio
async def test_disable_then_resume_does_not_revive_previously_queued_work(
    execution_setup,
):
    from scheduler.app.services.cron.managed.controls import ControlService
    from swe.app.crons.managed_results import ExecutionWriter

    db, caller, management, job_id = await setup_job(execution_setup)
    old = await add_run(db, caller, job_id, "old-period")
    controls = ControlService(db, management.platform)
    for name, enabled in (("pause", False), ("resume", True)):
        version = db.conn.execute(
            "SELECT management_version FROM swe_cron_job_states",
        ).fetchone()[0]
        await controls.set_enabled(
            caller,
            job_id,
            {
                "request_id": name,
                "enabled": enabled,
                "expected_management_version": version,
            },
        )
    writer, key = ExecutionWriter(db), caller.job_key(job_id)
    await writer.accept(old["execution_id"], key)
    assert await writer.start(old["execution_id"], key, "old-worker") is None
    new = await add_run(db, caller, job_id, "new-period")
    await writer.accept(new["execution_id"], key)
    assert (
        await writer.start(new["execution_id"], key, "new-worker") is not None
    )


@pytest.mark.asyncio
async def test_inflight_success_preserves_manual_pause_reason(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.controls import ControlService
    from swe.app.crons import managed_results

    db, caller, management, job_id = await setup_job(execution_setup)
    row = await add_run(db, caller, job_id, "period")
    writer, key = managed_results.ExecutionWriter(db), caller.job_key(job_id)
    await writer.accept(row["execution_id"], key)
    await writer.start(row["execution_id"], key, "worker")
    version = db.conn.execute(
        "SELECT management_version FROM swe_cron_job_states",
    ).fetchone()[0]
    await ControlService(db, management.platform).set_enabled(
        caller,
        job_id,
        {
            "request_id": "manual-pause",
            "enabled": False,
            "expected_management_version": version,
        },
    )
    monkeypatch.setattr(
        managed_results,
        "resolve_cron_unread_auto_pause_config",
        lambda _: SimpleNamespace(enabled=True, threshold=1),
    )
    await writer.complete(
        row["execution_id"],
        key,
        "worker",
        managed_results.ExecutionOutcome("success"),
    )
    assert (
        db.conn.execute(
            "SELECT pause_reason FROM swe_cron_job_states",
        ).fetchone()[0]
        == "manual"
    )
