# -*- coding: utf-8 -*-
"""只根据已确认的共库收敛事实提供新队列容量反馈。"""

from datetime import timedelta

import pytest

from tests.unit.scheduler.test_managed_cron_executions import execution_setup
from tests.unit.scheduler.test_managed_cron_settlement import completed


@pytest.mark.asyncio
async def test_feedback_waits_for_async_and_counts_observed_completion_once(
    execution_setup,
):
    from scheduler.app.services.cron.managed.feedback import (
        ManagedCapacityFeedback,
    )
    from scheduler.app.services.cron.managed.operations import utc_now

    db, _, _ = execution_setup
    row, history = await completed(execution_setup)
    feedback = ManagedCapacityFeedback(db)
    now = utc_now()
    args = {
        "since": now - timedelta(minutes=10),
        "now_utc": now + timedelta(minutes=1),
        "scope": {
            "source_id": "bank",
            "provider_id": "default",
            "model_id": "default",
        },
    }
    value = await feedback.summarize(**args)
    assert value["running_count"] == 1
    assert value["success_count"] == value["failure_count"] == 0
    await db.execute(
        "UPDATE swe_cron_executions SET async_status='error' WHERE id=%s",
        (history,),
    )
    assert await feedback.tick() == 1
    assert await feedback.tick() == 0
    value = await feedback.summarize(**args)
    assert value["running_count"] == 0
    assert value["failure_count"] == 1
    assert (
        await db.fetch_one(
            "SELECT status FROM swe_cron_execution_runs WHERE execution_id=%s",
            (row["execution_id"],),
        )
    )["status"] == "success"


@pytest.mark.asyncio
async def test_managed_only_scopes_are_visible_but_legacy_protocol_is_not(
    execution_setup,
):
    from scheduler.app.services.cron.managed.feedback import (
        ManagedCapacityFeedback,
    )
    from scheduler.app.services.cron.managed.operations import utc_now

    db, _, _ = execution_setup
    row, _ = await completed(execution_setup)
    feedback = ManagedCapacityFeedback(db)
    scopes = await feedback.scopes(now_utc=utc_now())
    assert scopes == [
        {"source_id": "bank", "provider_id": "default", "model_id": "default"},
    ]
    await db.execute(
        "UPDATE swe_cron_execution_runs SET dispatch_meta_json='{}' WHERE execution_id=%s",
        (row["execution_id"],),
    )
    assert await feedback.scopes(now_utc=utc_now()) == []


@pytest.mark.asyncio
async def test_feedback_is_scope_isolated_and_uses_settled_time_not_primary_time(
    execution_setup,
):
    from scheduler.app.services.cron.managed.feedback import (
        ManagedCapacityFeedback,
    )
    from scheduler.app.services.cron.managed.operations import utc_now

    db, _, _ = execution_setup
    _, _history = await completed(execution_setup, "success")
    feedback = ManagedCapacityFeedback(db)
    await feedback.tick()
    now = utc_now()
    args = {
        "since": now - timedelta(seconds=30),
        "now_utc": now + timedelta(seconds=1),
    }
    scope = {
        "source_id": "bank",
        "provider_id": "default",
        "model_id": "default",
    }
    assert (await feedback.summarize(**args, scope=scope))[
        "success_count"
    ] == 1
    assert (
        await feedback.summarize(
            **args,
            scope={**scope, "source_id": "another"},
        )
    )["success_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_scope_exists", [False, True])
async def test_one_capacity_controller_merges_old_and_new_feedback_once(
    execution_setup,
    legacy_scope_exists,
):
    from datetime import datetime, timezone
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from scheduler.app.services.cron.managed.feedback import (
        ManagedCapacityFeedback,
    )
    from scheduler.app.services.cron.scheduling_service import (
        CronSchedulingService,
        WorkerScope,
        WorkerStrategy,
    )
    from tests.unit.scheduler.test_cron_scheduling_service import (
        _DispatchStore,
    )

    db, caller, _ = execution_setup
    run, _ = await completed(execution_setup, "error")
    now = datetime.now(timezone.utc)
    await ExecutionStore(db).enqueue(
        caller,
        run["job_id"],
        ExecutionRequest("ordinary", "next", scheduled_at=now),
    )
    legacy = _DispatchStore([])
    legacy.scopes = (
        [WorkerScope(source_id="bank")] if legacy_scope_exists else []
    )
    legacy.latest_capacity = None
    legacy.feedback = {"success_count": 9, "failure_count": 0}
    legacy.strategy = WorkerStrategy(
        min_workers=1,
        baseline_workers=4,
        max_workers=8,
        adjust_interval_seconds=60,
        feedback_window_seconds=60,
        error_rate_rules=[
            {"min_error_rate": 0.001, "operation": "multiply", "value": 0.5},
        ],
    )
    runtime = CronSchedulingService(
        dispatch_store=legacy,
        worker_id="controller",
    )
    runtime.bind_managed_feedback(ManagedCapacityFeedback(db))
    assert await runtime.adjust_worker_capacity_if_due(
        now_utc=now + timedelta(seconds=1),
    )
    assert len(legacy.capacity) == 1
    decision = legacy.capacity[0]
    assert decision["source_id"] == "bank"
    assert decision["success_count"] == 9
    assert decision["failure_count"] == 1
    assert decision["error_rate"] == 0.1
    assert decision["effective_workers"] == 2
    assert not await runtime.adjust_worker_capacity_if_due(
        now_utc=now + timedelta(seconds=2),
    )


@pytest.mark.asyncio
async def test_partial_feedback_failure_never_publishes_half_a_capacity_decision(
    execution_setup,
):
    from datetime import datetime, timezone
    from scheduler.app.services.cron.managed.feedback import (
        ManagedCapacityFeedback,
    )
    from scheduler.app.services.cron.scheduling_service import (
        CronSchedulingService,
        WorkerScope,
    )
    from tests.unit.scheduler.test_cron_scheduling_service import (
        _DispatchStore,
    )

    db, _, _ = execution_setup
    await completed(execution_setup)
    legacy = _DispatchStore([])
    legacy.scopes = [WorkerScope(source_id="bank")]
    legacy.latest_capacity = None
    new = ManagedCapacityFeedback(db)

    async def unavailable(**_kwargs):
        raise RuntimeError("sensitive_password_must_not_escape")

    new.summarize = unavailable
    runtime = CronSchedulingService(dispatch_store=legacy)
    runtime.bind_managed_feedback(new)
    with pytest.raises(
        RuntimeError,
        match="cron_managed_feedback_unavailable",
    ) as error:
        await runtime.adjust_worker_capacity_if_due(
            now_utc=datetime.now(timezone.utc),
        )
    assert "sensitive_password" not in str(error.value)
    assert legacy.capacity == []


@pytest.mark.asyncio
async def test_failed_parallel_feedback_reaps_the_other_read(execution_setup):
    import asyncio
    from datetime import datetime, timezone
    from scheduler.app.services.cron.managed.feedback import (
        ManagedCapacityFeedback,
    )
    from scheduler.app.services.cron.scheduling_service import (
        CronSchedulingService,
        WorkerScope,
    )
    from tests.unit.scheduler.test_cron_scheduling_service import (
        _DispatchStore,
    )

    db, _, _ = execution_setup
    await completed(execution_setup)
    legacy = _DispatchStore([])
    legacy.scopes = [WorkerScope(source_id="bank")]
    legacy.latest_capacity = None
    started, cleaned, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def slow_old(**_kwargs):
        started.set()
        try:
            await release.wait()
            return {}
        finally:
            cleaned.set()

    async def failed_new(**_kwargs):
        await started.wait()
        raise ConnectionError("private_connection_details")

    legacy.summarize_recent_completion_feedback = slow_old
    new = ManagedCapacityFeedback(db)
    new.summarize = failed_new
    runtime = CronSchedulingService(dispatch_store=legacy)
    runtime.bind_managed_feedback(new)
    try:
        with pytest.raises(
            RuntimeError,
            match="cron_managed_feedback_unavailable",
        ):
            await runtime.adjust_worker_capacity_if_due(
                now_utc=datetime.now(timezone.utc),
            )
        assert cleaned.is_set()
    finally:
        release.set()
        await asyncio.sleep(0)
