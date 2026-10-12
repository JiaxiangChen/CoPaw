# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit.app.test_external_cron_scope_refresh import (
    CronManager,
    CapturingSchedulerAdapter,
    _JobsRepo,
    _sample_job,
    _decode_job_param,
)


@pytest.mark.parametrize("existing_batch", [False, True])
@pytest.mark.parametrize("cron", ["0 9 * * *", "*/15 * * * *"])
async def test_zero_window_registers_or_updates_batch_at_original_time(
    existing_batch,
    cron,
):
    adapter = CapturingSchedulerAdapter()
    job = _sample_job(external_id="42")
    job.schedule.cron = cron
    if existing_batch:
        job.meta.update(
            {
                "batch_dispatch_external_job_id": "43",
                "batch_dispatch_offset_window_hours": 4,
            }
        )
    manager = CronManager(
        repo=_JobsRepo([job]),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id="tenant-a",
        scheduler_adapter=adapter,
    )
    manager._resolve_batch_dispatch_execution_model_params = lambda spec: {}

    saved = await manager._sync_batch_dispatch_scheduler_job(
        job,
        offset_window_hours=0,
    )

    path, payload = adapter.requests[0]
    assert path.endswith("/update-job" if existing_batch else "/add-job")
    assert payload["jobCron"] == f"0 {cron[:-1]}?"
    params = _decode_job_param(payload["jobParam"])
    assert params["batch_dispatch_offset_minutes"] == 0
    assert saved.meta["batch_dispatch_offset_window_hours"] == 0
    assert saved.meta["batch_dispatch_cron"] == cron
    assert "batch_dispatch_cron_warning" not in saved.meta


@pytest.mark.asyncio
async def test_disabled_parent_keeps_batch_wakeup_timer_enabled(monkeypatch):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    adapter = CapturingSchedulerAdapter()
    job = _sample_job(external_id="42").model_copy(update={"enabled": False})
    manager = CronManager(
        repo=_JobsRepo([job]),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id="tenant-a",
        scheduler_adapter=adapter,
    )
    manager._resolve_batch_dispatch_execution_model_params = lambda spec: {}
    await manager._sync_batch_dispatch_scheduler_job(
        job,
        offset_window_hours=4,
    )
    states = [
        payload["runFlag"]
        for path, payload in adapter.requests
        if path.endswith("/update-job-run-states")
    ]
    assert states[-1] == 1


@pytest.mark.asyncio
async def test_enable_requires_persisted_control_initialization(monkeypatch):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    adapter = CapturingSchedulerAdapter()
    job = _sample_job(external_id="42")
    manager = CronManager(
        repo=_JobsRepo([job]),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id="tenant-a",
        scheduler_adapter=adapter,
    )
    manager._monitor_sync_client = None
    manager._resolve_batch_dispatch_execution_model_params = lambda spec: {}
    with pytest.raises(RuntimeError, match="定义同步未启用"):
        await manager.enable_batch_dispatch_for_parent(job.id)
