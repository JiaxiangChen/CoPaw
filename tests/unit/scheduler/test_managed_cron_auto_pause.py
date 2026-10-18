# -*- coding: utf-8 -*-
"""执行层只写自动暂停事实，管理层收敛定义/投影且旧动作不能覆盖恢复。"""

import json
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.unit.scheduler.test_managed_cron_executions import execution_setup
from tests.unit.scheduler.test_managed_cron_management import create_payload


async def paused_job(setup, monkeypatch, mode="ordinary", actor=None):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from swe.app.crons import managed_results

    db, caller, management = setup
    payload = create_payload()
    payload["dispatch_state"] = mode
    created = await management.create(caller, payload)
    if actor is None:
        run, _ = await ExecutionStore(db).enqueue(
            caller,
            created["job_id"],
            ExecutionRequest(
                "ordinary" if mode == "ordinary" else "batch",
                "period",
                scheduled_at=datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
            ),
        )
    else:
        from scheduler.app.services.cron.managed.identity import CallerIdentity
        from scheduler.app.services.cron.managed.scheduling import (
            ManagedSchedulingService,
        )
        from tests.unit.scheduler.test_managed_cron_callbacks import body

        callback = body(db, created)
        run = await ManagedSchedulingService(
            db,
            clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
        ).accept(
            CallerIdentity(
                caller.tenant_id,
                caller.source_id,
                caller.agent_id,
                actor,
            ),
            callback,
            "ordinary",
        )
    writer, key = managed_results.ExecutionWriter(db), caller.job_key(
        created["job_id"],
    )
    await writer.accept(run["execution_id"], key)
    await writer.start(run["execution_id"], key, "worker")
    monkeypatch.setattr(
        managed_results,
        "resolve_cron_unread_auto_pause_config",
        lambda _: SimpleNamespace(enabled=True, threshold=1),
    )
    await writer.complete(
        run["execution_id"],
        key,
        "worker",
        managed_results.ExecutionOutcome("success", preview="result"),
    )
    state = dict(
        db.conn.execute(
            "SELECT * FROM swe_cron_job_states WHERE job_id=?",
            (created["job_id"],),
        ).fetchone(),
    )
    return created, state


@pytest.mark.asyncio
async def test_auto_pause_updates_definition_and_projection_preserving_swe_fact(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.auto_pause import (
        AutoPauseController,
    )

    db, caller, management = execution_setup
    job, before = await paused_job(execution_setup, monkeypatch)
    await AutoPauseController(db, management.platform).tick()
    row = db.conn.execute("SELECT * FROM swe_cron_jobs_v2").fetchone()
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    assert not row["enabled"]
    assert not json.loads(row["spec_json"])["enabled"]
    assert state["pause_reason"] == "auto_unread_threshold"
    assert state["auto_paused_at"] == before["auto_paused_at"]
    assert (
        state["queue_invalidation_version"]
        == before["queue_invalidation_version"]
    )
    assert not db.conn.execute("SELECT enabled FROM swe_cron_jobs").fetchone()[
        0
    ]
    operation = db.conn.execute(
        "SELECT * FROM swe_cron_registration_journal WHERE action='auto_pause'",
    ).fetchone()
    assert operation["actor_id"] == caller.actor_id
    assert operation["status"] == "completed"
    assert "pause" in management.platform.calls


@pytest.mark.asyncio
async def test_root_auto_pause_never_closes_whole_batch_entrance(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.auto_pause import (
        AutoPauseController,
    )

    db, _, management = execution_setup
    await paused_job(execution_setup, monkeypatch, "batch_enabled")
    count = management.platform.calls.count("pause")
    await AutoPauseController(db, management.platform).tick()
    assert not db.conn.execute(
        "SELECT enabled FROM swe_cron_jobs_v2",
    ).fetchone()[0]
    assert management.platform.calls.count("pause") == count


@pytest.mark.asyncio
async def test_failed_auto_pause_can_be_superseded_by_explicit_resume(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.auto_pause import (
        AutoPauseController,
    )
    from scheduler.app.services.cron.managed.controls import ControlService

    db, caller, management = execution_setup
    job, _ = await paused_job(execution_setup, monkeypatch)
    management.platform.fail_enable = True
    controller = AutoPauseController(db, management.platform)
    await controller.tick()
    operation = db.conn.execute(
        "SELECT * FROM swe_cron_registration_journal WHERE action='auto_pause'",
    ).fetchone()
    assert operation["status"] == "failed"
    management.platform.fail_enable = False
    version = db.conn.execute(
        "SELECT management_version FROM swe_cron_job_states",
    ).fetchone()[0]
    controls = ControlService(db, management.platform)
    resumed = await controls.set_enabled(
        caller,
        job["job_id"],
        {
            "request_id": "resume-after-auto-failure",
            "enabled": True,
            "expected_management_version": version,
        },
    )
    assert resumed["status"] == "completed"
    calls = list(management.platform.calls)
    await controller.tick()
    stale = await controls.retry(
        caller,
        job["job_id"],
        operation["operation_id"],
    )
    assert stale["status"] == "superseded"
    assert list(management.platform.calls) == calls
    assert db.conn.execute("SELECT enabled FROM swe_cron_jobs_v2").fetchone()[
        0
    ]
    assert (
        db.conn.execute(
            "SELECT pause_reason FROM swe_cron_job_states",
        ).fetchone()[0]
        is None
    )


@pytest.mark.asyncio
async def test_stale_fact_observed_before_resume_cannot_disable_again(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.auto_pause import (
        AutoPauseController,
    )
    from scheduler.app.services.cron.managed.controls import ControlService

    db, caller, management = execution_setup
    job, fact = await paused_job(execution_setup, monkeypatch)
    version = db.conn.execute(
        "SELECT management_version FROM swe_cron_job_states",
    ).fetchone()[0]
    await ControlService(db, management.platform).set_enabled(
        caller,
        job["job_id"],
        {
            "request_id": "resume-before-sweep",
            "enabled": True,
            "expected_management_version": version,
        },
    )
    await AutoPauseController(db, management.platform).apply_fact(
        caller.job_key(job["job_id"]),
        fact["queue_invalidation_version"],
        fact["auto_paused_at"],
    )
    assert db.conn.execute("SELECT enabled FROM swe_cron_jobs_v2").fetchone()[
        0
    ]
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_registration_journal WHERE action='auto_pause'",
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_background_keeps_causal_actor_without_forging_manager_role(
    monkeypatch,
):
    from tests.unit.scheduler.test_managed_cron_callbacks import (
        CallbackDatabase,
    )
    from scheduler.app.services.cron.managed.auto_pause import (
        AutoPauseController,
    )
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )
    from tests.unit.scheduler.test_managed_cron_management import (
        PlatformClient,
        ResourceClient,
    )

    db = CallbackDatabase()
    caller = CallerIdentity("alice", "bank", "default", "alice")
    management = ManagementService(db, ResourceClient(), PlatformClient())
    try:
        await paused_job(
            (db, caller, management),
            monkeypatch,
            actor="platform-operator",
        )
        await AutoPauseController(db, management.platform).tick()
        row = db.conn.execute(
            "SELECT * FROM swe_cron_registration_journal WHERE action='auto_pause'",
        ).fetchone()
        assert row["actor_id"] == "platform-operator"
        assert row["status"] == "completed"
        assert (
            db.conn.execute(
                "SELECT creator_user_id FROM swe_cron_jobs_v2",
            ).fetchone()[0]
            == "alice"
        )
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_failed_auto_pause_does_not_prevent_explicit_deletion(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.auto_pause import (
        AutoPauseController,
    )

    db, caller, management = execution_setup
    job, _ = await paused_job(execution_setup, monkeypatch)
    management.platform.fail_enable = True
    await AutoPauseController(db, management.platform).tick()
    management.platform.fail_enable = False
    result = await management.delete(
        caller,
        job["job_id"],
        {
            "request_id": "delete-after-auto-failure",
            "expected_definition_version": 1,
        },
    )
    assert result["logical_delete_applied"]
    assert result["status"] == "pending"
    assert db.conn.execute(
        "SELECT deleted_at FROM swe_cron_jobs_v2",
    ).fetchone()[0]
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    db.conn.execute(
        "UPDATE swe_cron_executions SET async_status='success' WHERE status='success'",
    )
    await ExecutionSettlement(db).tick()
    result = await management.retry(
        caller,
        job["job_id"],
        result["operation_id"],
    )
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_interrupted_auto_pause_resumes_original_operation_without_running_job(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.auto_pause import (
        AutoPauseController,
    )

    db, _, management = execution_setup
    _, fact = await paused_job(execution_setup, monkeypatch)
    entered, never = asyncio.Event(), asyncio.Event()
    original = management.platform.set_enabled

    async def blocked(*_args):
        entered.set()
        await never.wait()

    monkeypatch.setattr(management.platform, "set_enabled", blocked)
    task = asyncio.create_task(
        AutoPauseController(db, management.platform).tick(),
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    first = db.conn.execute(
        "SELECT operation_id FROM swe_cron_registration_journal WHERE action='auto_pause'",
    ).fetchone()[0]
    db.conn.execute(
        "UPDATE swe_cron_registration_journal SET lease_until='2000-01-01 00:00:00' WHERE action='auto_pause'",
    )
    monkeypatch.setattr(management.platform, "set_enabled", original)
    await AutoPauseController(db, management.platform).tick()
    current = db.conn.execute(
        "SELECT * FROM swe_cron_registration_journal WHERE action='auto_pause'",
    ).fetchone()
    assert current["operation_id"] == first
    assert current["status"] == "completed"
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 1
    )
    assert (
        db.conn.execute(
            "SELECT unread_execution_count FROM swe_cron_job_states",
        ).fetchone()[0]
        == 1
    )
    assert (
        db.conn.execute(
            "SELECT auto_paused_at FROM swe_cron_job_states",
        ).fetchone()[0]
        == fact["auto_paused_at"]
    )


@pytest.mark.asyncio
async def test_runtime_tick_processes_auto_pause_lane(monkeypatch):
    from tests.unit.scheduler.test_managed_cron_dispatch_state import (
        ModeDatabase,
        ModePlatform,
    )
    from tests.unit.scheduler.test_managed_cron_management import (
        ResourceClient,
    )
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )
    from tests.unit.scheduler.test_managed_cron_recovery import (
        RecoveryClient,
        worker,
    )

    db = ModeDatabase()
    caller = CallerIdentity("alice", "bank", "default", "alice")
    management = ManagementService(db, ResourceClient(), ModePlatform())
    try:
        await paused_job((db, caller, management), monkeypatch)
        result = await worker(db, management, RecoveryClient(db)).tick()
        assert result["auto_pause"] == 1
        assert not db.conn.execute(
            "SELECT enabled FROM swe_cron_jobs_v2",
        ).fetchone()[0]
    finally:
        db.conn.close()
