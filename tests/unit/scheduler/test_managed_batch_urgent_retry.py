# -*- coding: utf-8 -*-
"""已闭批的人工重跑加急，但未闭批重试和已有容量边界不改变。"""

import pytest

from scheduler.app.services.cron.managed.batch_retry import BatchRetryService
from tests.unit.scheduler.test_managed_batch_retry import (
    failed_batch,
    retry_body,
)
from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup


@pytest.mark.asyncio
async def test_closed_batch_retry_is_urgent_and_preserves_original_batch(
    callback_setup,
):
    db = callback_setup[0]
    caller, batch_id = await failed_batch(callback_setup)
    db.conn.execute(
        "UPDATE swe_cron_managed_batch_members SET business_attempt=3",
    )
    service = BatchRetryService(db)
    accepted = await service.retry(
        caller,
        batch_id,
        retry_body(await service.preview(caller, batch_id)),
    )
    identifier = accepted["results"][0]["execution_id"]
    run = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (identifier,),
    )
    assert run["dispatch_lane"] == "urgent"
    import json

    event = await db.fetch_one(
        "SELECT details_json FROM swe_cron_managed_dispatch_events WHERE execution_id=%s AND event_type='queued'",
        (identifier,),
    )
    assert json.loads(event["details_json"]).get("dispatch_lane") == "urgent"
    member = await db.fetch_one("SELECT * FROM swe_cron_managed_batch_members")
    assert member["batch_id"] == batch_id
    assert member["current_execution_id"] == identifier
    assert member["business_attempt"] == 4


@pytest.mark.asyncio
async def test_retry_while_native_retry_is_pending_remains_normal(
    callback_setup,
):
    db = callback_setup[0]
    caller, batch_id = await failed_batch(callback_setup)
    service = BatchRetryService(db)
    result = await service.retry(
        caller,
        batch_id,
        retry_body(await service.preview(caller, batch_id)),
    )
    run = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (result["results"][0]["execution_id"],),
    )
    assert run["dispatch_lane"] == "normal"


@pytest.mark.asyncio
async def test_urgent_http_retry_attempts_dispatch_after_commit(
    callback_setup,
):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from scheduler.app.routers import managed_batch_operations
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from tests.unit.scheduler.test_managed_cron_dispatcher import SweClient

    db = callback_setup[0]
    caller, batch_id = await failed_batch(callback_setup)
    db.conn.execute(
        "UPDATE swe_cron_managed_batch_members SET business_attempt=3",
    )
    service = BatchRetryService(db)
    command = retry_body(await service.preview(caller, batch_id))
    client = SweClient(db)
    app = FastAPI()
    app.state.managed_cron_worker = SimpleNamespace(
        dispatcher=ExecutionDispatcher(db, client),
    )
    app.include_router(managed_batch_operations.router, prefix="/api")
    app.dependency_overrides[managed_batch_operations.batch_service] = (
        lambda: service
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        result = await http.post(
            f"/api/scheduler/cron/managed-dispatch/batches/{batch_id}/retry",
            headers={
                "X-User-Id": "alice",
                "X-Source-Id": "bank",
                "X-User-Role": "manager",
            },
            json=command.model_dump(),
        )
    assert result.status_code == 200
    identifier = result.json()["results"][0]["execution_id"]
    actual = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (identifier,),
    )
    assert actual["status"] == "accepted"
    assert client.calls == [identifier]


@pytest.mark.asyncio
async def test_native_retry_after_urgent_attempt_keeps_urgent_lane(
    callback_setup,
):
    import json
    from datetime import datetime, timedelta
    from scheduler.app.services.cron.managed.auto_retry import AutoBatchRetry
    from tests.unit.scheduler.test_managed_batch_auto_retry import complete_run

    db = callback_setup[0]
    caller, batch_id = await failed_batch(callback_setup)
    db.conn.execute(
        "UPDATE swe_cron_executions SET error_message='技能任务结果不可用'",
    )
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET result_json=?",
        (json.dumps({"error_detail": "技能任务结果不可用"}),),
    )
    service = BatchRetryService(db)
    accepted = await service.retry(
        caller,
        batch_id,
        retry_body(await service.preview(caller, batch_id), stopped=True),
    )
    identifier = accepted["results"][0]["execution_id"]
    run = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (identifier,),
    )
    assert run["dispatch_lane"] == "urgent"
    await complete_run(db, run)
    ended = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (identifier,),
    )
    assert (
        await AutoBatchRetry(
            db,
            clock=lambda: datetime.fromisoformat(ended["finished_at"])
            + timedelta(minutes=6),
        ).tick()
        == 1
    )
    member = await db.fetch_one("SELECT * FROM swe_cron_managed_batch_members")
    retry = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (member["current_execution_id"],),
    )
    assert retry["dispatch_lane"] == "urgent"
